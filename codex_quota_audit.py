#!/usr/bin/env python3
"""Codex quota audit v2.8.

Analyze local Codex rollout logs and relate observed token usage to Codex quota
meters. The main historical analysis defaults to the 7-day meter; the Guardian /
auto-review audit also discovers 5-hour telemetry when present. Nothing leaves
your machine.

Quick start
-----------
Analysis only, no third-party dependencies:

    python3 codex_quota_audit.py

Charts require matplotlib. A virtual environment is recommended:

    python3 -m venv .venv
    source .venv/bin/activate
    python3 -m pip install matplotlib
    python3 codex_quota_audit.py --charts

Useful commands:

    python3 codex_quota_audit.py --help
    python3 codex_quota_audit.py --self-test
    python3 codex_quota_audit.py --charts --chart-all-regimes
    python3 codex_quota_audit.py --export-chart-data quota_chart_data.csv
    python3 codex_quota_audit.py --export-buckets quota_buckets.csv
    python3 codex_quota_audit.py --export-resets reset_ledger.csv
    python3 codex_quota_audit.py --export-guardian-buckets guardian_buckets.csv
    python3 codex_quota_audit.py --export-approval-episodes approval_episodes.csv

What it does
------------
* Reconstructs effective quota resets while separating near-zero resets_at churn.
* Uses high-water accounting so stale/backward meter readings do not double-count quota.
* Detects replayed rollout history from cumulative total_token_usage and excludes it by default.
* Tracks model and reasoning effort, including provenance/conflict diagnostics.
* Pairs codex-auto-review / Guardian inference with likely parent work sessions.
* Groups Guardian calls into approval episodes and measures incremental inference overhead.
* Associates approval episodes with conservative 5-hour / 7-day quota envelopes.
* Detects explicit linkage metadata when present, otherwise uses confidence-labelled temporal matching.
* Discovers and analyzes 5-hour and 7-day quota snapshots when present.
* Compares model/month and detected model-policy regimes.
* Estimates token-type quota weights only when the data are identifiable enough to support them.
* Produces model x effort chart data with whole-episode bootstrap intervals.
* With --charts, writes the model/effort chart and a Guardian approval-overhead chart when enough paired episodes exist.

Raw tokens/cache/model mix are direct observations. API-dollar values use public
list-price equivalents only as a normalization ruler; they are never plan billing.

By default the script reads ~/.codex/sessions and ~/.codex/archived_sessions.
The normal text analysis uses only the Python standard library. matplotlib is
imported only when --charts is requested.

Price JSON accepts either form:
    {"gpt-x": [4.0, 0.4, 20.0]}
    {"gpt-x": {"input": 4.0, "cached": 0.4, "output": 20.0}}
All prices are dollars per 1M tokens.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import glob
import json
import math
import os
import random
import re
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


__version__ = "2.8"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PRICES: Dict[str, Tuple[float, float, float]] = {
    "gpt-6-astra": (10.0, 1.0, 50.0),
    "gpt-5.6-sol": (4.0, 0.4, 20.0),
    "gpt-5.6-terra": (2.0, 0.2, 12.0),
    "gpt-5.6-luna": (0.2, 0.02, 1.2),
    "gpt-5.5": (5.0, 0.5, 30.0),
    "gpt-5.4": (2.5, 0.25, 15.0),
}

DEFAULT_WINDOW_MINUTES = 10080
DEFAULT_DENSE_THRESHOLD = 20
DEFAULT_REPLAY_SCAN_SECONDS = 2.0
DEFAULT_REPLAY_MIN_EVENTS = 20
DEFAULT_REPLAY_MIN_GROWTH_MTOKENS = 5.0
DEFAULT_REPLAY_START_MAX_MTOKENS = 2.0
DEFAULT_RESET_TOLERANCE_SECONDS = 60
DEFAULT_RESET_CLASS_TOLERANCE_HOURS = 2.0
DEFAULT_MIN_PRICE_COVERAGE = 0.95
DEFAULT_MODEL_PURITY = 0.90
DEFAULT_NOOP_USAGE_MAX = 1.0
DEFAULT_EFFECTIVE_RESET_DROP = 2.0
DEFAULT_WEIGHT_MODEL_PURITY = 0.95
DEFAULT_WEIGHT_MIN_POINTS = 40.0
DEFAULT_WEIGHT_MIN_EPISODES = 5
DEFAULT_WEIGHT_BOOTSTRAPS = 200
DEFAULT_WEIGHT_MAX_CONDITION = 50.0
DEFAULT_WEIGHT_MAX_CV_WAPE = 0.40
DEFAULT_WEIGHT_CV_TOLERANCE = 0.03
DEFAULT_REGIME_MIN_EPISODES = 3
DEFAULT_REGIME_MIN_POINTS = 20.0
DEFAULT_REGIME_MIN_RATIO = 1.30
DEFAULT_REGIME_MIN_IMPROVEMENT = 0.45
DEFAULT_CHART_MODEL_PURITY = 0.95
DEFAULT_CHART_EFFORT_PURITY = 0.95
DEFAULT_CHART_MIN_POINTS = 10.0
DEFAULT_CHART_MIN_EPISODES = 3
DEFAULT_CHART_BOOTSTRAPS = 1000
DEFAULT_CHART_INTERVAL = 0.80
DEFAULT_GUARDIAN_PURITY = 0.95
DEFAULT_GUARDIAN_ISOLATION_SECONDS = 60.0
DEFAULT_GUARDIAN_WINDOWS = (300, 10080)
DEFAULT_GUARDIAN_PARENT_MATCH_SECONDS = 300.0
DEFAULT_GUARDIAN_EPISODE_GAP_SECONDS = 300.0
DEFAULT_GUARDIAN_CONTEXT_SECONDS = 120.0
DEFAULT_GUARDIAN_QUOTA_SNAPSHOT_SECONDS = 600.0
DEFAULT_GUARDIAN_MATCH_TOKEN_RATIO = 2.0
DEFAULT_GUARDIAN_FIT_BOOTSTRAPS = 300
EPS = 1e-9


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Usage:
    uncached: int = 0
    cached: int = 0
    output: int = 0
    api_usd: Optional[float] = None

    @property
    def input_total(self) -> int:
        return self.uncached + self.cached

    @property
    def tokens(self) -> int:
        return self.uncached + self.cached + self.output


@dataclass(frozen=True)
class RateWindow:
    used: float
    reset_at: float


@dataclass
class SessionLinkInfo:
    """Privacy-safe linkage fingerprints discovered inside one rollout file.

    Values are one-way hashes of ID-like metadata. Raw identifiers are never
    retained or printed by the analyzer.
    """
    own_ids: set[str] = field(default_factory=set)
    parent_ids: set[str] = field(default_factory=set)
    schema_paths: Counter = field(default_factory=Counter)


@dataclass
class GuardianPair:
    guardian_source: str
    parent_source: Optional[str]
    confidence: str
    method: str
    nearest_seconds: float = float("nan")
    coverage_60s: float = 0.0
    ambiguous: bool = False


@dataclass
class Event:
    ts_raw: str
    ts: datetime
    source: str
    model: str
    uncached: int
    cached: int
    output: int
    used: float
    reset_at: float
    window_minutes: int
    priced: bool
    api_usd: Optional[float]
    effort: str = "unknown"
    reasoning_output: int = 0
    approval_policy: str = "unknown"
    approvals_reviewer: str = "unknown"
    source_kind: str = "unknown"
    rate_windows: Dict[int, RateWindow] = field(default_factory=dict)
    total_input: Optional[int] = None
    total_cached: Optional[int] = None
    total_output: Optional[int] = None
    source_index: int = 0
    probable_replay: bool = False
    uncertain_dense: bool = False

    @property
    def cumulative_tokens(self) -> Optional[int]:
        if self.total_input is None or self.total_output is None:
            return None
        return self.total_input + self.total_output

    @property
    def tokens(self) -> int:
        return self.uncached + self.cached + self.output

    @property
    def input_total(self) -> int:
        return self.uncached + self.cached

    @property
    def is_auto_review_inference(self) -> bool:
        return self.model == "codex-auto-review"

    @property
    def is_confirmed_guardian(self) -> bool:
        return self.is_auto_review_inference and self.source_kind == "subagent"

    @property
    def activity_class(self) -> str:
        if self.is_auto_review_inference:
            return "auto-review inference"
        if self.approvals_reviewer == "auto_review":
            return "auto-review parent"
        if self.approvals_reviewer == "user":
            return "user-review parent"
        if self.source_kind == "subagent":
            return "other subagent"
        return "other"


@dataclass
class ParseStats:
    files: int = 0
    lines: int = 0
    json_errors: int = 0
    token_count_records: int = 0
    missing_usage: int = 0
    missing_target_limit: int = 0
    immediate_duplicate_totals: int = 0
    candidate_events: int = 0
    global_duplicates: int = 0
    replay_prefixes: int = 0
    replay_events: int = 0
    replay_tokens: int = 0
    uncertain_dense_groups: int = 0
    uncertain_dense_events: int = 0
    analyzed_events: int = 0
    effort_direct_observations: int = 0
    effort_fallback_observations: int = 0
    effort_conflicts: int = 0
    effort_state_updates: int = 0
    unknown_effort_events: int = 0
    target_candidate_events: int = 0
    window_records: Dict[int, int] = field(default_factory=dict)
    source_kind_updates: int = 0
    reviewer_state_updates: int = 0
    approval_policy_updates: int = 0
    session_links: Dict[str, SessionLinkInfo] = field(default_factory=dict)
    approval_markers: Dict[str, List[Tuple[datetime, str]]] = field(default_factory=dict)
    link_schema_paths: Counter = field(default_factory=Counter)


@dataclass
class Bucket:
    reset_key: int
    reset_at: float
    start_ts: datetime
    end_ts: datetime
    start_used: float
    end_used: float
    points: float
    events: int
    usage: Usage
    priced_tokens: int
    total_tokens: int
    model_tokens: Dict[str, int] = field(default_factory=dict)
    effort_tokens: Dict[str, int] = field(default_factory=dict)
    activity_tokens: Dict[str, int] = field(default_factory=dict)
    activity_events: Dict[str, int] = field(default_factory=dict)
    reviewer_tokens: Dict[str, int] = field(default_factory=dict)

    @property
    def price_coverage(self) -> float:
        return self.priced_tokens / self.total_tokens if self.total_tokens else 1.0

    @property
    def cache_ratio(self) -> float:
        denom = self.usage.uncached + self.usage.cached
        return self.usage.cached / denom if denom else float("nan")

    @property
    def dominant_model(self) -> Tuple[str, float]:
        total = sum(self.model_tokens.values())
        if not total:
            return ("unknown", 0.0)
        model, n = max(self.model_tokens.items(), key=lambda kv: kv[1])
        return (model, n / total)

    @property
    def dominant_effort(self) -> Tuple[str, float]:
        total = sum(self.effort_tokens.values())
        if not total:
            return ("unknown", 0.0)
        effort, n = max(self.effort_tokens.items(), key=lambda kv: kv[1])
        return (effort, n / total)


@dataclass
class EpisodeSummary:
    reset_key: int
    reset_at: float
    activation_at: datetime
    first_ts: datetime
    last_ts: datetime
    first_used: float
    last_used: float
    high_used: float
    new_high_points: float
    adjacent_down_steps: int
    backstep_observations: int
    max_backstep: float
    events: int
    usage: Usage
    priced_tokens: int
    total_tokens: int
    model_tokens: Dict[str, int]
    buckets: List[Bucket]

    @property
    def price_coverage(self) -> float:
        return self.priced_tokens / self.total_tokens if self.total_tokens else 1.0

    @property
    def cache_ratio(self) -> float:
        denom = self.usage.uncached + self.usage.cached
        return self.usage.cached / denom if denom else float("nan")

    @property
    def first_seen_lag(self) -> timedelta:
        return self.first_ts - self.activation_at


@dataclass
class ResetRecord:
    raw_index: int
    kind: str
    creates_episode: bool
    activation_at: datetime
    new_due_at: datetime
    previous_accounting_due_at: Optional[datetime]
    relative_to_previous_due: Optional[timedelta]
    first_seen_at: datetime
    first_seen_lag: timedelta
    before_used: Optional[float]
    after_used: float
    observed_drop: Optional[float]
    raw_reset_key: int
    accounting_reset_key: int


@dataclass
class Analysis:
    events: List[Event]
    raw_episodes: List[EpisodeSummary]
    episodes: List[EpisodeSummary]
    ledger: List[ResetRecord]
    buckets: List[Bucket]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def epoch_to_local(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone()


def reset_key(reset_at: float, tolerance_seconds: int) -> int:
    """Cluster resets_at values to a small tolerance without losing the median value."""
    if tolerance_seconds <= 1:
        return int(round(reset_at))
    return int(round(reset_at / tolerance_seconds) * tolerance_seconds)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_short_dt(dt: datetime) -> str:
    return dt.strftime("%m-%d %H:%M")


def fmt_pct_ratio(x: float, digits: int = 0) -> str:
    if x != x:
        return "n/a"
    return f"{100*x:.{digits}f}%"


def fmt_hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600.0
    if abs(hours) < 24:
        return f"{hours:+.1f}h"
    return f"{hours/24:+.2f}d"


def weighted_quantile(values: Sequence[Tuple[float, float]], q: float) -> float:
    vals = sorted((v, w) for v, w in values if w > 0 and v == v)
    if not vals:
        return float("nan")
    target = q * sum(w for _, w in vals)
    acc = 0.0
    for value, weight in vals:
        acc += weight
        if acc + EPS >= target:
            return value
    return vals[-1][0]


def top_model_mix(model_tokens: Dict[str, int], min_share: float = 0.05) -> str:
    total = sum(model_tokens.values())
    if not total:
        return "n/a"
    parts = []
    for model, n in sorted(model_tokens.items(), key=lambda kv: (-kv[1], kv[0])):
        share = n / total
        if share >= min_share:
            parts.append(f"{model} {share:.0%}")
    return ", ".join(parts) if parts else "n/a"


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

def load_prices(path: Optional[str]) -> Dict[str, Tuple[float, float, float]]:
    prices = dict(DEFAULT_PRICES)
    if not path:
        return prices
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError("price file must contain a JSON object")
    for model, value in obj.items():
        if isinstance(value, (list, tuple)) and len(value) == 3:
            triple = tuple(float(x) for x in value)
        elif isinstance(value, dict):
            triple = (float(value["input"]), float(value["cached"]), float(value["output"]))
        else:
            raise ValueError(f"invalid price for {model!r}")
        prices[str(model)] = triple  # type: ignore[assignment]
    return prices


def price_event(model: str, uncached: int, cached: int, output: int,
                prices: Dict[str, Tuple[float, float, float]]) -> Optional[float]:
    p = prices.get(model)
    if p is None:
        return None
    return (uncached * p[0] + cached * p[1] + output * p[2]) / 1_000_000.0


# ---------------------------------------------------------------------------
# Parsing and filtering
# ---------------------------------------------------------------------------

def session_files(home: str) -> List[str]:
    active = glob.glob(os.path.join(home, "sessions", "**", "*.jsonl"), recursive=True)
    archived = glob.glob(os.path.join(home, "archived_sessions", "*.jsonl"))
    return sorted(set(active + archived))


def _nested_get(obj: object, path: Sequence[str]) -> object:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _normalize_effort(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value or None


def effort_from_payload(payload: object, stats: Optional[ParseStats] = None) -> Optional[str]:
    """Extract reasoning effort from a log payload.

    The direct payload.effort field is preferred. Collaboration/thread settings
    are fallbacks because real logs frequently repeat the same state there. If
    multiple non-null representations disagree we audit the conflict and still
    use the documented precedence rather than dropping the state entirely.
    """
    if not isinstance(payload, dict):
        return None
    direct = _normalize_effort(payload.get("effort"))
    fallbacks = [
        _normalize_effort(_nested_get(payload, ("collaboration_mode", "settings", "reasoning_effort"))),
        _normalize_effort(_nested_get(payload, ("thread_settings", "reasoning_effort"))),
        _normalize_effort(_nested_get(payload, ("thread_settings", "collaboration_mode", "settings", "reasoning_effort"))),
    ]
    vals = [v for v in [direct] + fallbacks if v is not None]
    if not vals:
        return None
    if stats is not None:
        if direct is not None:
            stats.effort_direct_observations += 1
        else:
            stats.effort_fallback_observations += 1
        if len(set(vals)) > 1:
            stats.effort_conflicts += 1
    return direct or next(v for v in fallbacks if v is not None)


def _normalize_state(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def model_from_payload(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("model"),
        _nested_get(payload, ("state", "model")),
        _nested_get(payload, ("thread_settings", "model")),
        _nested_get(payload, ("collaboration_mode", "settings", "model")),
        _nested_get(payload, ("state", "collaboration_mode", "model")),
    ]
    for value in candidates:
        value2 = _normalize_state(value)
        if value2:
            return value2
    return None


def approval_state_from_payload(payload: object) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(payload, dict):
        return None, None
    policy = (
        _normalize_state(payload.get("approval_policy"))
        or _normalize_state(_nested_get(payload, ("thread_settings", "approval_policy")))
    )
    reviewer = (
        _normalize_state(payload.get("approvals_reviewer"))
        or _normalize_state(_nested_get(payload, ("thread_settings", "approvals_reviewer")))
    )
    return policy, reviewer


def source_kind_from_payload(payload: object) -> Optional[str]:
    """Return a privacy-safe coarse session source such as cli or subagent."""
    if not isinstance(payload, dict):
        return None
    src = payload.get("source")
    if isinstance(src, str):
        src = src.strip().lower()
        return src or None
    if isinstance(src, dict):
        # Real Guardian rollouts use source={subagent:{...}}. We intentionally do
        # not retain nested IDs, paths, names, or other source metadata.
        if "subagent" in src:
            return "subagent"
        if "guardian" in src:
            return "guardian"
        kind = src.get("type") or src.get("kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip().lower()
    return None


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _id_fingerprint(value: object) -> Optional[str]:
    """Return a privacy-safe fingerprint for an ID-like scalar."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if len(text) < 6 or len(text) > 256:
        return None
    # Paths, prompts, and prose are deliberately rejected. Identifiers are
    # expected to be compact scalars without path separators or long whitespace.
    if "/" in text or "\\" in text or "\n" in text or "\r" in text:
        return None
    if len(text.split()) > 2:
        return None
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24]


def _rollout_fingerprint(path: str) -> Optional[str]:
    m = _UUID_RE.search(os.path.basename(path))
    return _id_fingerprint(m.group(0)) if m else None


def _collect_link_ids(payload: object, info: SessionLinkInfo, stats: Optional[ParseStats] = None) -> None:
    """Discover likely session/thread linkage IDs without retaining raw values.

    Codex schemas evolve. Rather than hard-code one parent field, this walks only
    metadata already selected by the parser and fingerprints ID-like fields whose
    paths mention session/thread/conversation/rollout/parent/subagent. Values under
    source.subagent are treated as parent-side linkage because that object describes
    how a spawned subagent relates to its origin.
    """
    domain = ("session", "thread", "conversation", "rollout", "parent", "subagent", "agent")

    def walk(obj: object, path: Tuple[str, ...], depth: int) -> None:
        if depth > 7:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k).lower()
                p2 = path + (key,)
                walk(v, p2, depth + 1)
        elif isinstance(obj, list):
            for v in obj[:64]:
                walk(v, path + ("[]",), depth + 1)
        else:
            if not path:
                return
            terminal = path[-1]
            path_text = ".".join(path)
            idish = (
                terminal.endswith("_id")
                or terminal in {"session", "thread", "conversation", "rollout"}
                or (terminal == "id" and any(w in path_text for w in domain))
            )
            if not idish or not any(w in path_text for w in domain):
                return
            fp = _id_fingerprint(obj)
            if not fp:
                return
            schema = ".".join(path[-5:])
            info.schema_paths[schema] += 1
            if stats is not None:
                stats.link_schema_paths[schema] += 1
            if "parent" in path_text or "source.subagent" in path_text:
                info.parent_ids.add(fp)
            else:
                info.own_ids.add(fp)

    walk(payload, tuple(), 0)


def _approval_marker_kind(payload: object, obj_type: object = None) -> Optional[str]:
    """Detect explicit approval request/decision records without reading content."""
    if not isinstance(payload, dict):
        return None
    ptype = str(payload.get("type") or obj_type or "").strip().lower()
    if "thread_settings" in ptype:
        return None
    keys: List[str] = []

    def collect_keys(x: object, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                keys.append(str(k).lower())
                if isinstance(v, (dict, list)):
                    collect_keys(v, depth + 1)
        elif isinstance(x, list):
            for v in x[:32]:
                collect_keys(v, depth + 1)

    collect_keys(payload)
    signals = " ".join([ptype] + keys)
    if "approval" not in signals:
        return None
    # Settings alone are state, not an approval episode marker.
    non_state = [k for k in keys if "approval" in k and k not in {"approval_policy", "approvals_reviewer"}]
    if "approval" not in ptype and not non_state:
        return None
    if any(w in signals for w in ("request", "requested", "prompt", "ask")):
        return "request"
    if any(w in signals for w in ("decision", "response", "approved", "denied", "reject", "accept")):
        return "decision"
    return "approval"


def extract_rate_windows(rate_limits: object) -> Dict[int, RateWindow]:
    out: Dict[int, RateWindow] = {}
    if not isinstance(rate_limits, dict) or rate_limits.get("limit_id") != "codex":
        return out
    for name in ("primary", "secondary"):
        win = rate_limits.get(name)
        if not isinstance(win, dict):
            continue
        try:
            minutes = int(win["window_minutes"])
            used = float(win["used_percent"])
            reset_at = float(win["resets_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if minutes > 0:
            out[minutes] = RateWindow(used=used, reset_at=reset_at)
    return out


def parse_file(path: str,
               prices: Dict[str, Tuple[float, float, float]],
               target_window_minutes: int,
               stats: ParseStats) -> List[Event]:
    """Parse one rollout file once, retaining every Codex rate-limit window.

    `used`/`reset_at` continue to refer to the requested main-analysis window so
    the existing weekly analysis remains backward compatible. `rate_windows`
    retains 5h/7d (and any future windows) for the Guardian audit.
    """
    model: Optional[str] = None
    effort: Optional[str] = None
    approval_policy: Optional[str] = None
    approvals_reviewer: Optional[str] = None
    source_kind: Optional[str] = None
    link_info = SessionLinkInfo()
    approval_markers: List[Tuple[datetime, str]] = []
    prev_total: Optional[Tuple[int, int, int]] = None
    out: List[Event] = []
    source_index = 0
    file_line_index = 0

    try:
        fh = open(path, "rb")
    except OSError:
        return out

    with fh:
        for raw in fh:
            file_line_index += 1
            stats.lines += 1
            # Session source usually appears at the beginning. Later state changes
            # are selected by narrow field-name checks to avoid JSON-decoding every
            # prompt/response/tool-content record in very large rollout files.
            is_usage_hint = b'"token_count"' in raw or b'"token_usage_record"' in raw
            metadata_hint = (
                file_line_index <= 50
                or b'"turn_context"' in raw
                or b'"effort"' in raw
                or b'"reasoning_effort"' in raw
                or b'"approval_policy"' in raw
                or b'"approvals_reviewer"' in raw
                or b'"thread_settings_applied"' in raw
                or b'"codex-auto-review"' in raw
                or b'"parent' in raw
                or b'"session_id"' in raw
                or b'"thread_id"' in raw
                or b'"conversation_id"' in raw
                or b'"rollout_id"' in raw
                or b'"subagent"' in raw
                or b'"approval' in raw
            )
            if not is_usage_hint and not metadata_hint:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                stats.json_errors += 1
                continue

            payload = obj.get("payload") or {}
            if not isinstance(payload, dict):
                continue

            _collect_link_ids(payload, link_info, stats)
            marker_kind = _approval_marker_kind(payload, obj.get("type"))
            if marker_kind is not None:
                try:
                    marker_ts = parse_timestamp(obj["timestamp"])
                    approval_markers.append((marker_ts, marker_kind))
                except (KeyError, TypeError, ValueError):
                    pass

            new_model = model_from_payload(payload)
            if new_model is not None:
                model = new_model

            new_effort = effort_from_payload(payload, stats)
            if new_effort is not None and new_effort != effort:
                effort = new_effort
                stats.effort_state_updates += 1

            new_policy, new_reviewer = approval_state_from_payload(payload)
            if new_policy is not None and new_policy != approval_policy:
                approval_policy = new_policy
                stats.approval_policy_updates += 1
            if new_reviewer is not None and new_reviewer != approvals_reviewer:
                approvals_reviewer = new_reviewer
                stats.reviewer_state_updates += 1

            new_source = source_kind_from_payload(payload)
            if new_source is not None and new_source != source_kind:
                source_kind = new_source
                stats.source_kind_updates += 1

            ptype = payload.get("type") or obj.get("type")
            if ptype not in ("token_count", "token_usage_record"):
                continue
            stats.token_count_records += 1

            info = payload.get("info") or {}
            if not isinstance(info, dict):
                info = {}
            last = info.get("last_token_usage") or payload.get("last_token_usage")
            total = info.get("total_token_usage") or payload.get("total_token_usage")
            if not isinstance(last, dict):
                stats.missing_usage += 1
                continue

            total_input = total_cached = total_output = None
            if isinstance(total, dict):
                cur_total = tuple(int(total.get(k, 0) or 0) for k in
                                  ("input_tokens", "cached_input_tokens", "output_tokens"))
                if cur_total == prev_total:
                    stats.immediate_duplicate_totals += 1
                    continue
                prev_total = cur_total
                total_input, total_cached, total_output = cur_total

            windows = extract_rate_windows(payload.get("rate_limits"))
            if not windows:
                stats.missing_target_limit += 1
                continue
            for minutes in windows:
                stats.window_records[minutes] = stats.window_records.get(minutes, 0) + 1

            target = windows.get(target_window_minutes)
            if target is None:
                stats.missing_target_limit += 1
                used = float("nan")
                reset_at = float("nan")
            else:
                stats.target_candidate_events += 1
                used = target.used
                reset_at = target.reset_at

            try:
                ts_raw = obj["timestamp"]
                ts = parse_timestamp(ts_raw)
            except (KeyError, TypeError, ValueError):
                stats.json_errors += 1
                continue

            inp = int(last.get("input_tokens", 0) or 0)
            cached = int(last.get("cached_input_tokens", 0) or 0)
            output = int(last.get("output_tokens", 0) or 0)
            reasoning_output = int(last.get("reasoning_output_tokens", 0) or 0)
            uncached = max(inp - cached, 0)
            current_model = model or "unknown"
            current_effort = effort or "unknown"
            if current_effort == "unknown":
                stats.unknown_effort_events += 1
            api_usd = price_event(current_model, uncached, cached, output, prices)

            out.append(Event(
                ts_raw=ts_raw,
                ts=ts,
                source=path,
                model=current_model,
                uncached=uncached,
                cached=cached,
                output=output,
                used=used,
                reset_at=reset_at,
                window_minutes=target_window_minutes,
                priced=api_usd is not None,
                api_usd=api_usd,
                effort=current_effort,
                reasoning_output=reasoning_output,
                approval_policy=approval_policy or "unknown",
                approvals_reviewer=approvals_reviewer or "unknown",
                source_kind=source_kind or "unknown",
                rate_windows=windows,
                total_input=total_input,
                total_cached=total_cached,
                total_output=total_output,
                source_index=source_index,
            ))
            source_index += 1
            stats.candidate_events += 1

    path_fp = _rollout_fingerprint(path)
    if path_fp:
        link_info.own_ids.add(path_fp)
    if link_info.own_ids or link_info.parent_ids or link_info.schema_paths:
        stats.session_links[path] = link_info
    if approval_markers:
        # De-duplicate identical marker records that are sometimes emitted twice.
        stats.approval_markers[path] = sorted(set(approval_markers), key=lambda x: x[0])
    return out

def event_identity(e: Event) -> Tuple[object, ...]:
    meter = tuple(
        sorted((minutes, round(win.used, 6), round(win.reset_at, 3))
               for minutes, win in e.rate_windows.items())
    )
    return (
        e.ts_raw,
        e.uncached,
        e.cached,
        e.output,
        e.total_input,
        e.total_cached,
        e.total_output,
        meter,
    )


def detect_replay_prefixes(events: List[Event], stats: ParseStats,
                           scan_seconds: float, min_events: int,
                           min_growth_mtokens: float, start_max_mtokens: float,
                           dense_threshold: int) -> None:
    """Identify session-history replay from cumulative-token reconstruction.

    Real samples show a distinctive prefix: a new rollout file emits thousands of
    token_count records in well under a second while cumulative total_token_usage
    rebuilds from near zero to hundreds of millions of tokens. Live events then
    continue from that rebuilt cumulative total at normal cadence.

    We exclude only prefixes with positive sequence evidence. Other dense seconds
    are merely flagged as uncertain and remain in the primary analysis.
    """
    by_file: Dict[str, List[Event]] = defaultdict(list)
    for e in events:
        by_file[e.source].append(e)

    replay_ids = set()
    for source, evs in by_file.items():
        evs.sort(key=lambda e: e.source_index)
        if not evs:
            continue
        first_ts = evs[0].ts
        prefix = [e for e in evs if 0 <= (e.ts - first_ts).total_seconds() <= scan_seconds]
        with_totals = [e for e in prefix if e.cumulative_tokens is not None]
        if len(prefix) < min_events or len(with_totals) < max(3, int(0.9 * len(prefix))):
            continue

        vals = [e.cumulative_tokens for e in with_totals]
        assert all(v is not None for v in vals)
        vals2 = [int(v) for v in vals if v is not None]
        monotone_pairs = sum(b >= a for a, b in zip(vals2, vals2[1:]))
        monotone_fraction = monotone_pairs / max(1, len(vals2) - 1)
        start_m = vals2[0] / 1e6
        growth_m = (vals2[-1] - vals2[0]) / 1e6

        # Rate-limit snapshots in a replay often encode historical states spanning
        # hours/days even though their outer log timestamps are compressed together.
        # Check every retained limit window so replay detection still works when the
        # requested main-analysis window is absent from part of history.
        historical_state_evidence = False
        windows_seen = sorted({m for e in prefix for m in e.rate_windows})
        for minutes in windows_seen:
            wins = [e.rate_windows[minutes] for e in prefix if minutes in e.rate_windows]
            reset_vals = [w.reset_at for w in wins if w.reset_at > 0]
            reset_span = (max(reset_vals) - min(reset_vals)) if len(reset_vals) >= 2 else 0.0
            used_vals = [w.used for w in wins]
            used_span = (max(used_vals) - min(used_vals)) if used_vals else 0.0
            if reset_span >= 60.0 or used_span >= 2.0:
                historical_state_evidence = True
                break

        # A large, monotone rebuild from near-zero cumulative usage in the opening
        # seconds is the core replay signature. Historical rate-limit variation is
        # corroborating evidence, but huge rebuilds are sufficient by themselves.
        is_replay = (
            start_m <= start_max_mtokens
            and growth_m >= min_growth_mtokens
            and monotone_fraction >= 0.98
            and (historical_state_evidence or growth_m >= 5 * min_growth_mtokens)
        )
        if is_replay:
            stats.replay_prefixes += 1
            for e in prefix:
                e.probable_replay = True
                replay_ids.add(id(e))
                stats.replay_events += 1
                stats.replay_tokens += e.tokens

    # Keep the old density idea only as a diagnostic. Dense groups that were not
    # proven replay remain included by default.
    if dense_threshold > 0:
        groups: Dict[Tuple[str, str], List[Event]] = defaultdict(list)
        for e in events:
            groups[(e.source, e.ts.strftime("%Y-%m-%dT%H:%M:%S"))].append(e)
        for group in groups.values():
            if len(group) <= dense_threshold or all(id(e) in replay_ids for e in group):
                continue
            stats.uncertain_dense_groups += 1
            for e in group:
                e.uncertain_dense = True
                stats.uncertain_dense_events += 1


def load_events(home: str,
                prices: Dict[str, Tuple[float, float, float]],
                target_window_minutes: int,
                replay_scan_seconds: float,
                replay_min_events: int,
                replay_min_growth_mtokens: float,
                replay_start_max_mtokens: float,
                dense_threshold: int) -> Tuple[List[Event], ParseStats]:
    """Return deduplicated events with evidence-based replay flags attached."""
    stats = ParseStats()
    paths = session_files(home)
    stats.files = len(paths)

    candidates: List[Event] = []
    for path in paths:
        candidates.extend(parse_file(path, prices, target_window_minutes, stats))
    candidates.sort(key=lambda e: (e.ts, e.ts_raw, e.source))

    by_identity: Dict[Tuple[object, ...], Event] = {}
    for e in candidates:
        ident = event_identity(e)
        prev = by_identity.get(ident)
        if prev is None:
            by_identity[ident] = e
        else:
            stats.global_duplicates += 1
            prev_quality = (
                int(prev.model != "unknown") + int(prev.effort != "unknown")
                + int(prev.approvals_reviewer != "unknown")
                + int(prev.approval_policy != "unknown")
                + int(prev.source_kind != "unknown")
            )
            new_quality = (
                int(e.model != "unknown") + int(e.effort != "unknown")
                + int(e.approvals_reviewer != "unknown")
                + int(e.approval_policy != "unknown")
                + int(e.source_kind != "unknown")
            )
            if new_quality > prev_quality:
                by_identity[ident] = e

    deduped = sorted(by_identity.values(), key=lambda e: (e.ts, e.ts_raw, e.source))
    detect_replay_prefixes(
        deduped, stats, replay_scan_seconds, replay_min_events,
        replay_min_growth_mtokens, replay_start_max_mtokens, dense_threshold,
    )
    return deduped, stats


def events_for_window(events: Sequence[Event], window_minutes: int) -> List[Event]:
    """Project deduplicated usage records onto one rate-limit window.

    The parser already projects the requested main window, so reuse those Event
    objects instead of cloning hundreds of thousands of records. Other windows
    are shallow copies sharing the immutable rate-window mapping contents.
    """
    out: List[Event] = []
    for e in events:
        win = e.rate_windows.get(window_minutes)
        if win is None:
            continue
        if e.window_minutes == window_minutes and e.used == e.used and e.reset_at == e.reset_at:
            out.append(e)
        else:
            out.append(replace(e, used=win.used, reset_at=win.reset_at, window_minutes=window_minutes))
    return out


def discovered_windows(events: Sequence[Event]) -> List[int]:
    return sorted({minutes for e in events for minutes in e.rate_windows})


# ---------------------------------------------------------------------------
# Episodes, high-water quota attribution, and reset ledger
# ---------------------------------------------------------------------------

def group_episodes(events: Sequence[Event], tolerance_seconds: int) -> Dict[int, List[Event]]:
    grouped: Dict[int, List[Event]] = defaultdict(list)
    for e in events:
        grouped[reset_key(e.reset_at, tolerance_seconds)].append(e)
    for evs in grouped.values():
        evs.sort(key=lambda e: (e.ts, e.ts_raw, e.source))
    return dict(grouped)


def make_bucket(reset_k: int, reset_at: float,
                start_ts: datetime, start_used: float,
                end_event: Event, events: Sequence[Event], points: float) -> Bucket:
    unc = sum(e.uncached for e in events)
    cached = sum(e.cached for e in events)
    output = sum(e.output for e in events)
    priced_tokens = sum(e.tokens for e in events if e.priced)
    total_tokens = sum(e.tokens for e in events)
    usd = sum(e.api_usd or 0.0 for e in events if e.priced)
    any_priced = any(e.priced for e in events)
    model_tokens: Dict[str, int] = defaultdict(int)
    effort_tokens: Dict[str, int] = defaultdict(int)
    activity_tokens: Dict[str, int] = defaultdict(int)
    activity_events: Dict[str, int] = defaultdict(int)
    reviewer_tokens: Dict[str, int] = defaultdict(int)
    for e in events:
        model_tokens[e.model] += e.tokens
        effort_tokens[e.effort] += e.tokens
        activity_tokens[e.activity_class] += e.tokens
        activity_events[e.activity_class] += 1
        reviewer_tokens[e.approvals_reviewer] += e.tokens
    return Bucket(
        reset_key=reset_k,
        reset_at=reset_at,
        start_ts=start_ts,
        end_ts=end_event.ts,
        start_used=start_used,
        end_used=end_event.used,
        points=points,
        events=len(events),
        usage=Usage(unc, cached, output, usd if any_priced else None),
        priced_tokens=priced_tokens,
        total_tokens=total_tokens,
        model_tokens=dict(model_tokens),
        effort_tokens=dict(effort_tokens),
        activity_tokens=dict(activity_tokens),
        activity_events=dict(activity_events),
        reviewer_tokens=dict(reviewer_tokens),
    )


def high_water_buckets(evs: Sequence[Event], reset_k: int, reset_at: float) -> List[Bucket]:
    """Attribute work only when used_percent reaches a new high within an episode.

    Lower readings do not lower the baseline. Their work remains pending and is
    attributed only if/when a later observation exceeds the previous high-water
    mark. This avoids double-counting repeated crossings after stale/backstep
    readings.
    """
    if not evs:
        return []

    first = evs[0]
    high = first.used
    high_ts = first.ts
    pending: List[Event] = [first] if abs(first.used) <= EPS else []
    buckets: List[Bucket] = []

    for e in evs[1:]:
        pending.append(e)
        if e.used > high + EPS:
            points = e.used - high
            buckets.append(make_bucket(
                reset_k, reset_at, high_ts, high, e, pending, points
            ))
            high = e.used
            high_ts = e.ts
            pending = []

    return buckets


def summarize_episode(reset_k: int, evs: Sequence[Event], window_minutes: int,
                      canonical_reset_at: Optional[float] = None) -> EpisodeSummary:
    reset_at = canonical_reset_at if canonical_reset_at is not None else statistics.median(e.reset_at for e in evs)
    activation_at = epoch_to_local(reset_at) - timedelta(minutes=window_minutes)

    running_high = evs[0].used
    adjacent_down_steps = 0
    backstep_observations = 0
    max_backstep = 0.0
    for i, e in enumerate(evs):
        if i and e.used < evs[i-1].used - EPS:
            adjacent_down_steps += 1
        if e.used < running_high - EPS:
            backstep_observations += 1
            max_backstep = max(max_backstep, running_high - e.used)
        running_high = max(running_high, e.used)

    unc = sum(e.uncached for e in evs)
    cached = sum(e.cached for e in evs)
    output = sum(e.output for e in evs)
    total_tokens = sum(e.tokens for e in evs)
    priced_tokens = sum(e.tokens for e in evs if e.priced)
    usd = sum(e.api_usd or 0.0 for e in evs if e.priced)
    any_priced = any(e.priced for e in evs)
    model_tokens: Dict[str, int] = defaultdict(int)
    for e in evs:
        model_tokens[e.model] += e.tokens

    high = max(e.used for e in evs)
    return EpisodeSummary(
        reset_key=reset_k,
        reset_at=reset_at,
        activation_at=activation_at,
        first_ts=evs[0].ts,
        last_ts=evs[-1].ts,
        first_used=evs[0].used,
        last_used=evs[-1].used,
        high_used=high,
        new_high_points=max(high - evs[0].used, 0.0),
        adjacent_down_steps=adjacent_down_steps,
        backstep_observations=backstep_observations,
        max_backstep=max_backstep,
        events=len(evs),
        usage=Usage(unc, cached, output, usd if any_priced else None),
        priced_tokens=priced_tokens,
        total_tokens=total_tokens,
        model_tokens=dict(model_tokens),
        buckets=high_water_buckets(evs, reset_k, reset_at),
    )


def boundary_before_used(prev_raw: EpisodeSummary, raw_grouped: Dict[int, List[Event]],
                         next_first_ts: datetime) -> Optional[float]:
    prev_events = raw_grouped.get(prev_raw.reset_key, [])
    candidates = [e for e in prev_events if e.ts <= next_first_ts]
    if candidates:
        return candidates[-1].used
    return prev_events[-1].used if prev_events else prev_raw.last_used


def build_accounting_episodes(raw_episodes: Sequence[EpisodeSummary],
                              raw_grouped: Dict[int, List[Event]],
                              window_minutes: int,
                              tolerance_hours: float,
                              noop_usage_max: float,
                              effective_reset_drop: float) -> Tuple[List[EpisodeSummary], List[ResetRecord]]:
    """Collapse harmless resets_at churn, preserving conservative accounting boundaries.

    A raw resets_at transition is classified as:
      * scheduled: activation is close to the current accounting due date;
      * early/after-due reset: observed used_percent drops materially;
      * churn: both boundary readings are near zero and there is no material drop;
      * ambiguous: everything else. Ambiguous transitions still start a fresh
        accounting episode so a possible real reset cannot cause under-counting.
    """
    if not raw_episodes:
        return [], []

    ordered = sorted(raw_episodes, key=lambda ep: (ep.activation_at, ep.reset_at, ep.first_ts))
    tol = timedelta(hours=tolerance_hours)

    accounting_groups: Dict[int, List[Event]] = {}
    canonical_reset_at: Dict[int, float] = {}
    ledger: List[ResetRecord] = []

    first = ordered[0]
    current_key = first.reset_key
    current_due_at = epoch_to_local(first.reset_at)
    accounting_groups[current_key] = list(raw_grouped[first.reset_key])
    canonical_reset_at[current_key] = first.reset_at
    ledger.append(ResetRecord(
        raw_index=0,
        kind="initial",
        creates_episode=True,
        activation_at=first.activation_at,
        new_due_at=epoch_to_local(first.reset_at),
        previous_accounting_due_at=None,
        relative_to_previous_due=None,
        first_seen_at=first.first_ts,
        first_seen_lag=first.first_ts - first.activation_at,
        before_used=None,
        after_used=first.first_used,
        observed_drop=None,
        raw_reset_key=first.reset_key,
        accounting_reset_key=current_key,
    ))

    prev_raw = first
    for i, raw in enumerate(ordered[1:], 1):
        before = boundary_before_used(prev_raw, raw_grouped, raw.first_ts)
        after = raw.first_used
        drop = None if before is None else before - after
        delta = raw.activation_at - current_due_at

        if abs(delta) <= tol:
            kind = "scheduled"
            creates = True
        elif drop is not None and drop + EPS >= effective_reset_drop:
            kind = "early" if delta < -tol else "after-due"
            creates = True
        elif (before is not None and before <= noop_usage_max + EPS
              and after <= noop_usage_max + EPS):
            kind = "churn"
            creates = False
        else:
            kind = "ambiguous"
            creates = True

        if creates:
            current_key = raw.reset_key
            current_due_at = epoch_to_local(raw.reset_at)
            accounting_groups[current_key] = list(raw_grouped[raw.reset_key])
            canonical_reset_at[current_key] = raw.reset_at
        else:
            accounting_groups[current_key].extend(raw_grouped[raw.reset_key])
            accounting_groups[current_key].sort(key=lambda e: (e.ts, e.ts_raw, e.source))

        ledger.append(ResetRecord(
            raw_index=i,
            kind=kind,
            creates_episode=creates,
            activation_at=raw.activation_at,
            new_due_at=epoch_to_local(raw.reset_at),
            previous_accounting_due_at=current_due_at if not creates else (
                raw.activation_at - delta if delta is not None else None
            ),
            relative_to_previous_due=delta,
            first_seen_at=raw.first_ts,
            first_seen_lag=raw.first_ts - raw.activation_at,
            before_used=before,
            after_used=after,
            observed_drop=drop,
            raw_reset_key=raw.reset_key,
            accounting_reset_key=current_key,
        ))
        prev_raw = raw

    # Fix previous_due on created records: the expression above becomes awkward
    # after current_due_at is updated. Reconstruct it deterministically by walking
    # the ledger and keeping the last accounting due date.
    prev_due: Optional[datetime] = None
    for rec in ledger:
        rec.previous_accounting_due_at = prev_due
        if rec.creates_episode:
            prev_due = rec.new_due_at

    episodes = [
        summarize_episode(k, sorted(evs, key=lambda e: (e.ts, e.ts_raw, e.source)),
                          window_minutes, canonical_reset_at[k])
        for k, evs in accounting_groups.items()
    ]
    episodes.sort(key=lambda ep: (ep.activation_at, ep.reset_at, ep.first_ts))
    return episodes, ledger


def analyze_events(events: Sequence[Event], args: argparse.Namespace) -> Analysis:
    raw_grouped = group_episodes(events, args.reset_tolerance)
    raw_episodes = [summarize_episode(k, raw_grouped[k], args.window_minutes) for k in raw_grouped]
    raw_episodes.sort(key=lambda ep: (ep.activation_at, ep.reset_at, ep.first_ts))
    episodes, ledger = build_accounting_episodes(
        raw_episodes, raw_grouped, args.window_minutes,
        args.reset_class_tolerance_hours, args.noop_usage_max,
        args.effective_reset_drop,
    )
    buckets = [b for ep in episodes for b in ep.buckets]
    return Analysis(list(events), raw_episodes, episodes, ledger, buckets)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def usable_bucket(b: Bucket, min_price_coverage: float) -> bool:
    return b.points > EPS and b.price_coverage + EPS >= min_price_coverage and b.usage.api_usd is not None


def episode_api_per_point(ep: EpisodeSummary, min_price_coverage: float) -> Tuple[float, float]:
    good = [b for b in ep.buckets if usable_bucket(b, min_price_coverage)]
    points = sum(b.points for b in good)
    usd = sum(b.usage.api_usd or 0.0 for b in good)
    return (usd / points if points > EPS else float("nan"), points)


def print_header(args: argparse.Namespace, stats: ParseStats, analysis: Analysis) -> None:
    kinds = Counter(r.kind for r in analysis.ledger[1:])
    total_backsteps = sum(ep.backstep_observations for ep in analysis.episodes)
    max_backstep = max((ep.max_backstep for ep in analysis.episodes), default=0.0)
    effective = kinds.get("scheduled", 0) + kinds.get("early", 0) + kinds.get("after-due", 0)

    print("Codex quota audit v2.7")
    print("======================")
    source_display = "~/.codex" if args.home == os.path.expanduser("~/.codex") else args.home
    print(f"Source: {source_display}")
    print(f"Target limit: {args.window_minutes} minutes ({args.window_minutes / 1440:.1f} days)")
    print("API $ values are list-price-equivalent normalization only, never plan billing.\n")

    print("Data audit")
    print("----------")
    print(f"files scanned:                 {stats.files:,}")
    print(f"token_count records seen:      {stats.token_count_records:,}")
    print(f"candidate events:              {stats.candidate_events:,}")
    print(f"target-window candidates:      {stats.target_candidate_events:,}")
    if stats.window_records:
        window_text = ", ".join(f"{window_label(m)}={n:,}" for m, n in sorted(stats.window_records.items()))
        print(f"rate-limit window records:     {window_text}")
    print(f"immediate duplicate totals:    {stats.immediate_duplicate_totals:,}")
    print(f"cross-file/event duplicates:   {stats.global_duplicates:,}")
    print(f"probable replay prefixes:      {stats.replay_prefixes:,}")
    print(f"probable replay events:        {stats.replay_events:,}" +
          (" (included in primary analysis)" if args.include_replays else " (excluded from primary analysis)"))
    if stats.replay_events:
        print(f"probable replay tokens:        {stats.replay_tokens / 1e6:,.1f}M")
    print(f"uncertain dense groups:        {stats.uncertain_dense_groups:,} (included)")
    if stats.uncertain_dense_events:
        print(f"uncertain dense events:        {stats.uncertain_dense_events:,}")
    print(f"events in primary analysis:    {stats.analyzed_events:,}")
    print(f"effort direct observations:    {stats.effort_direct_observations:,}")
    print(f"effort fallback observations:  {stats.effort_fallback_observations:,}")
    print(f"effort state conflicts:        {stats.effort_conflicts:,}")
    print(f"candidate events unknown effort:{stats.unknown_effort_events:>11,}")
    if stats.json_errors:
        print(f"JSON/schema parse issues:      {stats.json_errors:,}")

    print("\nReset audit")
    print("-----------")
    print(f"raw resets_at cohorts:         {len(analysis.raw_episodes):,}")
    print(f"accounting episodes:           {len(analysis.episodes):,}")
    print(f"effective resets:              {effective:,}")
    print(f"  scheduled/on-time:           {kinds.get('scheduled', 0):,}")
    print(f"  early:                       {kinds.get('early', 0):,}")
    print(f"  after-due:                   {kinds.get('after-due', 0):,}")
    print(f"ambiguous boundaries:          {kinds.get('ambiguous', 0):,}")
    print(f"no-op resets_at churn:         {kinds.get('churn', 0):,}")
    print(f"same-episode backstep reads:   {total_backsteps:,}")
    print(f"largest same-episode backstep: {max_backstep:.0f} percentage points")
    print("Effective early-reset timing is observable, but banked-reset vs OpenAI-forced")
    print("cause is not identifiable from used_percent + resets_at alone.")


def print_reset_ledger(ledger: Sequence[ResetRecord], show_churn: bool = False) -> None:
    print("\nReset ledger")
    print("------------")
    print("Scheduled/early/after-due rows are effective resets. Ambiguous rows remain")
    print("accounting boundaries conservatively. Near-zero no-op resets_at churn is merged")
    print("and hidden here by default; use --show-reset-churn to display it.\n")
    print(f"{'activation':<17} {'kind':<11} {'used before->after':>18} {'previous due':<17} {'new due':<17} {'vs due':>9} {'seen lag':>9}")
    for r in ledger:
        if r.kind == "churn" and not show_churn:
            continue
        before = "?" if r.before_used is None else f"{r.before_used:.0f}"
        used = f"{before}->{r.after_used:.0f}%"
        prev_due = "-" if r.previous_accounting_due_at is None else fmt_dt(r.previous_accounting_due_at)
        delta = "-" if r.relative_to_previous_due is None else fmt_hours(r.relative_to_previous_due)
        lag = fmt_hours(r.first_seen_lag)
        print(f"{fmt_dt(r.activation_at):<17} {r.kind:<11} {used:>18} {prev_due:<17} {fmt_dt(r.new_due_at):<17} {delta:>9} {lag:>9}")


def print_episodes(episodes: Sequence[EpisodeSummary], min_price_coverage: float) -> None:
    print("\nQuota episodes (high-water accounting)")
    print("--------------------------------------")
    print("Only increases above the prior high-water mark count as quota points. resets_at")
    print("churn merged into an episode cannot restart the high-water baseline.\n")
    print(
        f"{'activation':<17} {'observed':<15} {'used first/high':>15} {'hw pt':>6} {'back':>5} "
        f"{'maxdip':>6} {'api pt':>6} {'turns':>7} {'tok M':>8} {'cache':>6} {'tokcov':>6} {'API$/pt':>8}  models"
    )
    for ep in sorted(episodes, key=lambda x: x.activation_at):
        span = f"{ep.first_ts.strftime('%m-%d')}..{ep.last_ts.strftime('%m-%d')}"
        meter = f"{ep.first_used:.0f}/{ep.high_used:.0f}%"
        api_per, api_points = episode_api_per_point(ep, min_price_coverage)
        api_s = f"{api_per:.2f}" if api_per == api_per else "n/a"
        print(
            f"{fmt_dt(ep.activation_at):<17} {span:<15} {meter:>15} {ep.new_high_points:6.0f} "
            f"{ep.backstep_observations:5d} {ep.max_backstep:6.0f} {api_points:6.0f} {ep.events:7d} "
            f"{ep.total_tokens/1e6:8.1f} {fmt_pct_ratio(ep.cache_ratio):>6} {fmt_pct_ratio(ep.price_coverage):>6} "
            f"{api_s:>8}  {top_model_mix(ep.model_tokens)}"
        )


def month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def aggregate_bucket_metrics(bs: Sequence[Bucket], min_price_coverage: float) -> Dict[str, float]:
    points = sum(b.points for b in bs)
    total_tokens = sum(b.total_tokens for b in bs)
    unc = sum(b.usage.uncached for b in bs)
    cached = sum(b.usage.cached for b in bs)
    output = sum(b.usage.output for b in bs)
    good = [b for b in bs if usable_bucket(b, min_price_coverage)]
    priced_points = sum(b.points for b in good)
    usd = sum(b.usage.api_usd or 0.0 for b in good)
    api = usd / priced_points if priced_points > EPS else float("nan")
    vals = [((b.usage.api_usd or 0.0) / b.points, b.points) for b in good if b.points > EPS]
    return {
        "points": points,
        "tokens": total_tokens,
        "uncached": unc,
        "cached": cached,
        "output": output,
        "priced_points": priced_points,
        "api_per_point": api,
        "median": weighted_quantile(vals, 0.50),
        "p10": weighted_quantile(vals, 0.10),
        "p90": weighted_quantile(vals, 0.90),
    }


def monthly_bucket_metrics(buckets: Sequence[Bucket], min_price_coverage: float) -> Dict[str, Dict[str, float]]:
    by_month: Dict[str, List[Bucket]] = defaultdict(list)
    for b in buckets:
        by_month[month_key(b.end_ts)].append(b)
    return {mo: aggregate_bucket_metrics(bs, min_price_coverage) for mo, bs in by_month.items()}


def print_monthly(events: Sequence[Event], buckets: Sequence[Bucket], min_price_coverage: float) -> None:
    by_month_events: Dict[str, List[Event]] = defaultdict(list)
    for e in events:
        by_month_events[month_key(e.ts)].append(e)
    metrics = monthly_bucket_metrics(buckets, min_price_coverage)
    months = sorted(set(by_month_events) | set(metrics))

    print("\nMonthly trend (high-water buckets)")
    print("----------------------------------")
    print("Only new accounting-episode high-water marks contribute quota points.\n")
    print(f"{'month':<8} {'turns':>7} {'tok M':>9} {'cache':>6} {'hw pt':>7} {'priced pt':>9} {'API$/pt':>8} {'median':>8} {'p10-p90':>16}")

    for mo in months:
        evs = by_month_events.get(mo, [])
        m = metrics.get(mo, {"points": 0.0, "priced_points": 0.0, "api_per_point": float('nan'),
                             "median": float('nan'), "p10": float('nan'), "p90": float('nan')})
        total_tokens = sum(e.tokens for e in evs)
        inp = sum(e.input_total for e in evs)
        cached = sum(e.cached for e in evs)
        cache_ratio = cached / inp if inp else float("nan")
        rng = f"{m['p10']:.2f}-{m['p90']:.2f}" if m['p10'] == m['p10'] and m['p90'] == m['p90'] else "n/a"
        api = m['api_per_point']
        med = m['median']
        print(
            f"{mo:<8} {len(evs):7d} {total_tokens/1e6:9.1f} {fmt_pct_ratio(cache_ratio):>6} "
            f"{m['points']:7.0f} {m['priced_points']:9.0f} {(f'{api:.2f}' if api == api else 'n/a'):>8} "
            f"{(f'{med:.2f}' if med == med else 'n/a'):>8} {rng:>16}"
        )


def print_model_time(buckets: Sequence[Bucket], min_price_coverage: float,
                     model_purity: float, min_points: float) -> None:
    grouped: Dict[Tuple[str, str], List[Bucket]] = defaultdict(list)
    for b in buckets:
        model, share = b.dominant_model
        if share + EPS >= model_purity:
            grouped[(month_key(b.end_ts), model)].append(b)

    print("\nModel x month (dominant high-water buckets)")
    print("-----------------------------------------")
    print(f"Buckets require >= {model_purity:.0%} raw-token share from one model; rows below {min_points:.0f} quota points are omitted.")
    print("This avoids hiding quota-policy changes inside an all-history model average.\n")
    print(f"{'month':<8} {'model':<20} {'pt':>6} {'bkt':>5} {'Mtok/pt':>8} {'uncM/pt':>8} {'cacheM/pt':>10} {'outM/pt':>8} {'API$/pt':>8}")

    rows = []
    for (mo, model), bs in grouped.items():
        m = aggregate_bucket_metrics(bs, min_price_coverage)
        if m["points"] + EPS < min_points:
            continue
        rows.append((mo, model, bs, m))
    for mo, model, bs, m in sorted(rows):
        p = m["points"]
        api = m["api_per_point"]
        print(
            f"{mo:<8} {model:<20} {p:6.0f} {len(bs):5d} {m['tokens']/1e6/p:8.2f} "
            f"{m['uncached']/1e6/p:8.2f} {m['cached']/1e6/p:10.2f} {m['output']/1e6/p:8.3f} "
            f"{(f'{api:.2f}' if api == api else 'n/a'):>8}"
        )



# ---------------------------------------------------------------------------
# Identifiability-aware token-type quota-weight analysis (v2.4)
# ---------------------------------------------------------------------------

@dataclass
class WeightRow:
    bucket: Bucket
    episode_id: int
    model: str
    model_share: float

    @property
    def points(self) -> float:
        return self.bucket.points


@dataclass
class PolicyRegime:
    model: str
    index: int
    episode_ids: Tuple[int, ...]
    start_ts: datetime
    end_ts: datetime
    points: float
    episodes: int
    detection_basis: str
    metric_before_after: Optional[Tuple[float, float]] = None

    @property
    def label(self) -> str:
        return f"R{self.index}"


@dataclass
class CandidateFit:
    name: str
    feature_names: Tuple[str, ...]
    coefficients: Tuple[float, ...]
    condition: float
    cv_wape: float
    cv_bias: float
    bootstrap_coeffs: List[Tuple[float, ...]]
    status: str
    reason: str


WEIGHT_SPECS = {
    # coefficients are quota points per 1M tokens.
    "total": ("all",),
    # Output is merged with uncached tokens in this deliberately simpler model.
    "cache": ("noncached", "cached"),
    "io": ("input", "output"),
    "full": ("uncached", "cached", "output"),
}


def _feature_vector(row: WeightRow, spec: str) -> List[float]:
    u = row.bucket.usage.uncached / 1e6
    c = row.bucket.usage.cached / 1e6
    o = row.bucket.usage.output / 1e6
    if spec == "total":
        return [u + c + o]
    if spec == "cache":
        # Reduced model for the specific cached-vs-uncached-input question.
        # Output is intentionally omitted; held-out CV exposes when that simplification
        # is inadequate. The full model remains the unbiased three-component attempt.
        return [u, c]
    if spec == "io":
        return [u + c, o]
    if spec == "full":
        return [u, c, o]
    raise KeyError(spec)


def _nnls(rows: Sequence[WeightRow], spec: str) -> Tuple[float, ...]:
    names = WEIGHT_SPECS[spec]
    n = len(names)
    G = [[0.0] * n for _ in range(n)]
    h = [0.0] * n
    for row in rows:
        x = _feature_vector(row, spec)
        y = row.points
        for i in range(n):
            h[i] += x[i] * y
            for j in range(n):
                G[i][j] += x[i] * x[j]
    beta = [0.0] * n
    for _ in range(4000):
        max_change = 0.0
        for i in range(n):
            if G[i][i] <= EPS:
                continue
            other = sum(G[i][j] * beta[j] for j in range(n) if j != i)
            new = max(0.0, (h[i] - other) / G[i][i])
            max_change = max(max_change, abs(new - beta[i]))
            beta[i] = new
        if max_change < 1e-11:
            break
    return tuple(beta)


def _sym_eigenvalues(a: List[List[float]]) -> List[float]:
    """Jacobi eigenvalues for a tiny real symmetric matrix (1-3 dimensions)."""
    n = len(a)
    if n == 1:
        return [a[0][0]]
    m = [row[:] for row in a]
    for _ in range(80):
        p = q = 0
        best = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                if abs(m[i][j]) > best:
                    best = abs(m[i][j]); p, q = i, j
        if best < 1e-12:
            break
        app, aqq, apq = m[p][p], m[q][q], m[p][q]
        phi = 0.5 * math.atan2(2.0 * apq, aqq - app)
        c, sn = math.cos(phi), math.sin(phi)
        for k in range(n):
            if k in (p, q):
                continue
            mkp, mkq = m[k][p], m[k][q]
            m[k][p] = m[p][k] = c * mkp - sn * mkq
            m[k][q] = m[q][k] = sn * mkp + c * mkq
        m[p][p] = c*c*app - 2*c*sn*apq + sn*sn*aqq
        m[q][q] = sn*sn*app + 2*c*sn*apq + c*c*aqq
        m[p][q] = m[q][p] = 0.0
    return sorted(m[i][i] for i in range(n))


def _design_condition(rows: Sequence[WeightRow], spec: str) -> float:
    n = len(WEIGHT_SPECS[spec])
    if n == 1:
        return 1.0
    cols = [[] for _ in range(n)]
    for row in rows:
        x = _feature_vector(row, spec)
        for i, v in enumerate(x):
            cols[i].append(v)
    norms = [math.sqrt(sum(v*v for v in col)) for col in cols]
    if any(v <= EPS for v in norms):
        return float("inf")
    gram = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            gram[i][j] = sum(a*b for a, b in zip(cols[i], cols[j])) / (norms[i] * norms[j])
    eig = _sym_eigenvalues(gram)
    lo, hi = min(eig), max(eig)
    if lo <= 1e-10 or hi <= 0:
        return float("inf")
    return math.sqrt(hi / lo)


def _predict(row: WeightRow, spec: str, beta: Sequence[float]) -> float:
    return sum(a*b for a, b in zip(_feature_vector(row, spec), beta))


def _episode_cv(rows: Sequence[WeightRow], spec: str) -> Tuple[float, float]:
    by_ep: Dict[int, List[WeightRow]] = defaultdict(list)
    for r in rows:
        by_ep[r.episode_id].append(r)
    ids = sorted(by_ep)
    if len(ids) < 3:
        return float("nan"), float("nan")
    # Leave-one-episode-out for small samples; deterministic 5-fold episode CV for larger ones.
    folds: List[List[int]]
    if len(ids) <= 12:
        folds = [[i] for i in ids]
    else:
        k = min(5, len(ids))
        folds = [[] for _ in range(k)]
        for j, eid in enumerate(ids):
            folds[j % k].append(eid)
    abs_err = actual_sum = signed_err = 0.0
    for held in folds:
        held_set = set(held)
        train = [r for r in rows if r.episode_id not in held_set]
        if not train:
            continue
        beta = _nnls(train, spec)
        for eid in held:
            actual = sum(r.points for r in by_ep[eid])
            pred = sum(_predict(r, spec, beta) for r in by_ep[eid])
            abs_err += abs(pred - actual)
            signed_err += pred - actual
            actual_sum += actual
    if actual_sum <= EPS:
        return float("nan"), float("nan")
    return abs_err / actual_sum, signed_err / actual_sum


def _bootstrap_episode_coeffs(rows: Sequence[WeightRow], spec: str, boots: int,
                              seed: int = 7) -> List[Tuple[float, ...]]:
    by_ep: Dict[int, List[WeightRow]] = defaultdict(list)
    for r in rows:
        by_ep[r.episode_id].append(r)
    ids = sorted(by_ep)
    if not ids or boots <= 0:
        return []
    rng = random.Random(seed)
    out = []
    for _ in range(boots):
        sampled: List[WeightRow] = []
        for _j in ids:
            sampled.extend(by_ep[rng.choice(ids)])
        out.append(_nnls(sampled, spec))
    return out


def _q(values: Sequence[float], p: float) -> float:
    vals = sorted(v for v in values if v == v and math.isfinite(v))
    if not vals:
        return float("nan")
    pos = p * (len(vals) - 1)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    f = pos - lo
    return vals[lo] * (1-f) + vals[hi] * f


def _ratio_interval(samples: Sequence[Tuple[float, ...]], num: int, den: int) -> Tuple[float, float, float, float]:
    vals = []
    valid = 0
    for b in samples:
        if den < len(b) and num < len(b) and b[den] > 1e-10:
            valid += 1
            vals.append(b[num] / b[den])
    frac = valid / len(samples) if samples else 0.0
    return _q(vals, .10), _q(vals, .50), _q(vals, .90), frac


def _coefficient_interval(samples: Sequence[Tuple[float, ...]], idx: int) -> Tuple[float, float, float]:
    vals = [b[idx] for b in samples if idx < len(b)]
    return _q(vals, .10), _q(vals, .50), _q(vals, .90)


def _fit_candidate(rows: Sequence[WeightRow], spec: str, boots: int,
                   max_condition: float, max_cv_wape: float) -> CandidateFit:
    beta = _nnls(rows, spec)
    cond = _design_condition(rows, spec)
    cv, bias = _episode_cv(rows, spec)
    bs = _bootstrap_episode_coeffs(rows, spec, boots)
    status, reason = "supported", ""
    if not math.isfinite(cond) or cond > max_condition:
        status, reason = "not identifiable", f"collinearity (condition {cond:.1f})"
    elif cv != cv:
        status, reason = "not identifiable", "too few independent episodes for held-out validation"
    elif cv > max_cv_wape:
        status, reason = "weak", f"episode CV error {cv:.0%}"
    elif len(beta) > 1:
        # Require stable relative coefficients. A coefficient at the NNLS boundary in
        # most bootstraps is a sign that the decomposition is not separately identified.
        base = 0
        for j in range(1, len(beta)):
            lo, med, hi, frac = _ratio_interval(bs, j, base)
            if frac < 0.80 or med != med:
                status, reason = "not identifiable", "reference coefficient is unstable/near zero in bootstrap fits"
                break
            if lo <= 0 and hi > 0:
                status, reason = "not identifiable", "relative coefficient repeatedly hits the non-negative boundary"
                break
            if lo > 0 and hi / lo > 10:
                status, reason = "not identifiable", "bootstrap relative-weight interval spans >10x"
                break
    return CandidateFit(spec, tuple(WEIGHT_SPECS[spec]), beta, cond, cv, bias, bs, status, reason)


def _dominant_weight_rows(buckets: Sequence[Bucket], purity: float) -> List[WeightRow]:
    rows = []
    for b in buckets:
        if b.points <= EPS:
            continue
        model, share = b.dominant_model
        if model == "unknown" or share + EPS < purity:
            continue
        rows.append(WeightRow(b, b.reset_key, model, share))
    return rows


def _weighted_log_sse(items: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    # items are (positive metric, weight)
    vals = [(math.log(v), w) for v, w in items if v > 0 and w > 0]
    if not vals:
        return float("nan"), float("nan")
    sw = sum(w for _, w in vals)
    mu = sum(x*w for x, w in vals) / sw
    sse = sum(w * (x-mu)**2 for x, w in vals)
    return mu, sse


def _episode_metric_rows(rows: Sequence[WeightRow], min_price_coverage: float) -> Tuple[List[Dict[str, object]], str]:
    by_ep: Dict[int, List[WeightRow]] = defaultdict(list)
    for r in rows:
        by_ep[r.episode_id].append(r)
    records = []
    priced_count = 0
    for eid, rs in by_ep.items():
        pts = sum(r.points for r in rs)
        if pts <= EPS:
            continue
        toks = sum(r.bucket.total_tokens for r in rs)
        usable = [r.bucket for r in rs if usable_bucket(r.bucket, min_price_coverage)]
        ppts = sum(b.points for b in usable)
        usd = sum(b.usage.api_usd or 0.0 for b in usable)
        api = usd / ppts if ppts >= max(1.0, pts * 0.8) and usd > 0 else float("nan")
        if api == api:
            priced_count += 1
        records.append({
            "episode_id": eid,
            "start": min(r.bucket.start_ts for r in rs),
            "end": max(r.bucket.end_ts for r in rs),
            "points": pts,
            "api": api,
            "tokens_per_point": toks / 1e6 / pts,
        })
    records.sort(key=lambda d: (d["start"], d["episode_id"]))
    # Never mix API-dollar and raw-token units inside one change-point series.
    # Use API normalization only when every usable episode for this model is priced;
    # otherwise fall back to raw Mtok/pt for the entire model's regime detector.
    basis = "API$/pt" if records and priced_count == len(records) else "Mtok/pt"
    for d in records:
        d["metric"] = d["api"] if basis == "API$/pt" else d["tokens_per_point"]
    return records, basis


def _regime_splits(records: Sequence[Dict[str, object]], min_episodes: int,
                   min_points: float, min_ratio: float, min_improvement: float) -> List[Tuple[int, int]]:
    """Recursive change-point segmentation on weighted log quota-efficiency metrics."""
    def recurse(lo: int, hi: int) -> List[Tuple[int, int]]:
        n = hi - lo
        if n < 2 * min_episodes:
            return [(lo, hi)]
        segment = records[lo:hi]
        base_items = [(float(d["metric"]), float(d["points"])) for d in segment if float(d["metric"]) > 0]
        mu0, sse0 = _weighted_log_sse(base_items)
        if not math.isfinite(sse0) or sse0 <= 1e-9:
            return [(lo, hi)]
        best = None
        for cut in range(lo + min_episodes, hi - min_episodes + 1):
            left, right = records[lo:cut], records[cut:hi]
            lp = sum(float(d["points"]) for d in left)
            rp = sum(float(d["points"]) for d in right)
            if lp < min_points or rp < min_points:
                continue
            lm, ls = _weighted_log_sse([(float(d["metric"]), float(d["points"])) for d in left])
            rm, rs = _weighted_log_sse([(float(d["metric"]), float(d["points"])) for d in right])
            if not all(math.isfinite(x) for x in (lm, ls, rm, rs)):
                continue
            improvement = max(0.0, 1.0 - (ls + rs) / sse0)
            ratio = math.exp(abs(lm - rm))
            score = improvement * math.log(max(ratio, 1.000001))
            if ratio >= min_ratio and improvement >= min_improvement and (best is None or score > best[0]):
                best = (score, cut, lm, rm)
        if best is None:
            return [(lo, hi)]
        _, cut, _lm, _rm = best
        return recurse(lo, cut) + recurse(cut, hi)
    return recurse(0, len(records)) if records else []


def detect_policy_regimes(rows: Sequence[WeightRow], min_price_coverage: float,
                          min_episodes: int, min_points: float,
                          min_ratio: float, min_improvement: float) -> List[PolicyRegime]:
    by_model: Dict[str, List[WeightRow]] = defaultdict(list)
    for r in rows:
        by_model[r.model].append(r)
    regimes: List[PolicyRegime] = []
    for model, mrows in sorted(by_model.items()):
        records, basis = _episode_metric_rows(mrows, min_price_coverage)
        if not records:
            continue
        spans = _regime_splits(records, min_episodes, min_points, min_ratio, min_improvement)
        for idx, (lo, hi) in enumerate(spans, 1):
            part = records[lo:hi]
            ids = tuple(int(d["episode_id"]) for d in part)
            regimes.append(PolicyRegime(
                model=model, index=idx, episode_ids=ids,
                start_ts=min(d["start"] for d in part), end_ts=max(d["end"] for d in part),
                points=sum(float(d["points"]) for d in part), episodes=len(part),
                detection_basis=basis,
            ))
    return regimes


def _fmt_ratio_fit(fit: CandidateFit) -> str:
    b = fit.coefficients
    if fit.name == "total":
        if not b or b[0] <= EPS:
            return "n/a"
        return f"{1/b[0]:.2f} Mtok/pt"
    if not fit.bootstrap_coeffs:
        return "n/a"
    if fit.name == "cache":
        lo, med, hi, frac = _ratio_interval(fit.bootstrap_coeffs, 1, 0)
        return f"cached/uncached={med:.3f} ({lo:.3f}-{hi:.3f})" if med == med else "ratio n/a"
    if fit.name == "io":
        lo, med, hi, frac = _ratio_interval(fit.bootstrap_coeffs, 1, 0)
        return f"output/input={med:.2f} ({lo:.2f}-{hi:.2f})" if med == med else "ratio n/a"
    if fit.name == "full":
        cl, cm, ch, _ = _ratio_interval(fit.bootstrap_coeffs, 1, 0)
        ol, om, oh, _ = _ratio_interval(fit.bootstrap_coeffs, 2, 0)
        if cm != cm or om != om:
            return "relative weights n/a"
        return f"unc=1 cache={cm:.3f} ({cl:.3f}-{ch:.3f}) out={om:.2f} ({ol:.2f}-{oh:.2f})"
    return ""


def _choose_candidate(fits: Sequence[CandidateFit], tolerance: float) -> Optional[CandidateFit]:
    good = [f for f in fits if f.status == "supported" and f.cv_wape == f.cv_wape]
    if not good:
        good = [f for f in fits if f.status == "weak" and f.cv_wape == f.cv_wape]
    if not good:
        return None
    best_cv = min(f.cv_wape for f in good)
    complexity = {"total": 1, "cache": 2, "io": 2, "full": 3}
    near = [f for f in good if f.cv_wape <= best_cv + tolerance]
    return min(near, key=lambda f: (complexity[f.name], f.cv_wape, f.name))


def print_weight_analysis(buckets: Sequence[Bucket], args: argparse.Namespace) -> None:
    rows = _dominant_weight_rows(buckets, args.weight_model_purity)
    regimes = detect_policy_regimes(
        rows, args.min_price_coverage, args.regime_min_episodes,
        args.regime_min_points, args.regime_min_ratio, args.regime_min_improvement,
    )
    print("\nToken-type quota weights (experimental, identifiability-aware)")
    print("------------------------------------------------------------")
    print(f"Uses >= {args.weight_model_purity:.0%} model-pure high-water buckets. Policy regimes are detected")
    print("within each model before fitting. Coefficients are non-negative quota points per")
    print("1M tokens; validation holds out whole reset episodes and bootstraps whole episodes.")
    print("A full 3-weight model is not reported as trustworthy merely because it can be fit.\n")
    if not regimes:
        print("No model has enough dominant high-water data for regime/weight analysis.")
        return

    print("Detected model-policy regimes")
    print(f"{'model':<20} {'reg':<4} {'span':<23} {'ep':>4} {'pt':>7} {'break basis':>11}")
    for rg in regimes:
        span = f"{rg.start_ts.strftime('%m-%d')}..{rg.end_ts.strftime('%m-%d')}"
        print(f"{rg.model:<20} {rg.label:<4} {span:<23} {rg.episodes:4d} {rg.points:7.0f} {rg.detection_basis:>11}")

    print("\nWeight-fit summary")
    print(f"{'model/regime':<25} {'ep':>3} {'pt':>6} {'best supported model':<12} {'CV err':>7}  inference")
    for rg in regimes:
        regime_ids = set(rg.episode_ids)
        rr = [r for r in rows if r.model == rg.model and r.episode_id in regime_ids]
        pts = sum(r.points for r in rr)
        eps = len(set(r.episode_id for r in rr))
        label = f"{rg.model} {rg.label}"
        if eps < args.weight_min_episodes or pts < args.weight_min_points:
            print(f"{label:<25} {eps:3d} {pts:6.0f} {'n/a':<12} {'n/a':>7}  insufficient data")
            continue
        fits = [
            _fit_candidate(rr, spec, args.weight_bootstraps,
                           args.weight_max_condition, args.weight_max_cv_wape)
            for spec in ("total", "cache", "io", "full")
        ]
        chosen = _choose_candidate(fits, args.weight_cv_tolerance)
        if chosen is None:
            inference = "no stable candidate"
            cvtxt = "n/a"
            name = "n/a"
        else:
            inference = _fmt_ratio_fit(chosen)
            cvtxt = f"{chosen.cv_wape:.0%}"
            name = chosen.name
        print(f"{label:<25} {eps:3d} {pts:6.0f} {name:<12} {cvtxt:>7}  {inference}")

        # Always surface supported two-weight partial answers because these are often
        # identifiable even when the full three-component decomposition is not.
        for partial_name in ("cache", "io"):
            pf = next(f for f in fits if f.name == partial_name)
            if pf.status == "supported" and pf is not chosen:
                print(f"  {partial_name} partial: {_fmt_ratio_fit(pf)}; held-out CV={pf.cv_wape:.0%}")

        full = next(f for f in fits if f.name == "full")
        if full.status != "supported":
            cond = "inf" if not math.isfinite(full.condition) else f"{full.condition:.1f}"
            print(f"  full 3-weight model: {full.status} — {full.reason}; condition={cond}, CV={fmt_pct_ratio(full.cv_wape) if full.cv_wape == full.cv_wape else 'n/a'}")
        elif chosen is not full:
            print(f"  full 3-weight model is statistically usable but adds little held-out predictive value; {_fmt_ratio_fit(full)}")

        if args.weight_details:
            for f in fits:
                cond = "inf" if not math.isfinite(f.condition) else f"{f.condition:.1f}"
                cv = f"{f.cv_wape:.1%}" if f.cv_wape == f.cv_wape else "n/a"
                bias = f"{f.cv_bias:+.1%}" if f.cv_bias == f.cv_bias else "n/a"
                detail = _fmt_ratio_fit(f)
                suffix = f" [{f.status}" + (f": {f.reason}" if f.reason else "") + "]"
                print(f"    {f.name:<5} cond={cond:>6} CV={cv:>6} bias={bias:>7}  {detail}{suffix}")

    print("\nModel definitions: total=one weight for all tokens; cache=uncached vs cached input")
    print("(output omitted in that reduced model); io=all input vs output; full=uncached input + cached input + output.")
    print("Choose simpler models when their held-out error is within the configured tolerance of a more complex fit.")


def print_replay_sensitivity(filtered: Analysis, included: Analysis,
                            min_price_coverage: float) -> None:
    fm = monthly_bucket_metrics(filtered.buckets, min_price_coverage)
    im = monthly_bucket_metrics(included.buckets, min_price_coverage)
    common = []
    for mo in sorted(set(fm) & set(im)):
        a, b = fm[mo], im[mo]
        if a["priced_points"] < 5 or b["priced_points"] < 5:
            continue
        fa, ia = a["api_per_point"], b["api_per_point"]
        if fa != fa or ia != ia or abs(fa) <= EPS:
            continue
        common.append((mo, a, b, (ia / fa - 1.0) * 100.0))

    print("\nReplay-filter sensitivity")
    print("-------------------------")
    print("Compares evidence-based replay filtering with the same parsed logs including")
    print("probable replay prefixes. A large delta shows how badly replayed history would")
    print("inflate usage if it were counted as fresh work.\n")
    if not common:
        print("Not enough commonly priced quota points for a monthly sensitivity comparison.")
        return
    print(f"{'month':<8} {'pt filt':>8} {'pt incl':>8} {'$/pt filt':>10} {'$/pt incl':>10} {'delta':>9}")
    deltas = []
    for mo, a, b, delta in common:
        deltas.append(abs(delta))
        print(f"{mo:<8} {a['priced_points']:8.0f} {b['priced_points']:8.0f} {a['api_per_point']:10.2f} {b['api_per_point']:10.2f} {delta:8.1f}%")
    med = statistics.median(deltas)
    worst = max(deltas)
    affected = sum(d > 5 for d in deltas)
    if worst <= 5:
        verdict = "low"
    elif worst <= 20:
        verdict = "moderate"
    else:
        verdict = "high in affected months"
    print(f"Median absolute monthly change: {med:.1f}%; worst month: {worst:.1f}%; >5% months: {affected}/{len(deltas)} ({verdict}).")



# ---------------------------------------------------------------------------
# Guardian / auto-review audit
# ---------------------------------------------------------------------------

def window_label(minutes: int) -> str:
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "7d"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _event_token_totals(events: Sequence[Event]) -> Tuple[int, int, int, int]:
    unc = sum(e.uncached for e in events)
    cached = sum(e.cached for e in events)
    out = sum(e.output for e in events)
    reasoning = sum(e.reasoning_output for e in events)
    return unc, cached, out, reasoning


def _session_count(events: Sequence[Event]) -> int:
    return len({e.source for e in events})


def _cache_ratio_events(events: Sequence[Event]) -> float:
    unc, cached, _, _ = _event_token_totals(events)
    denom = unc + cached
    return cached / denom if denom else float("nan")


def _non_auto_review_timestamps(events: Sequence[Event]) -> List[float]:
    return sorted(e.ts.timestamp() for e in events
                  if e.tokens > 0 and not e.is_auto_review_inference)


def _has_nearby_non_auto_review(non_auto_times: Sequence[float],
                                start_ts: datetime, end_ts: datetime,
                                margin_seconds: float) -> bool:
    if not non_auto_times:
        return False
    lo = start_ts.timestamp() - margin_seconds
    hi = end_ts.timestamp() + margin_seconds
    i = bisect.bisect_left(non_auto_times, lo)
    return i < len(non_auto_times) and non_auto_times[i] <= hi


def _source_events(events: Sequence[Event]) -> Dict[str, List[Event]]:
    out: Dict[str, List[Event]] = defaultdict(list)
    for e in events:
        out[e.source].append(e)
    for evs in out.values():
        evs.sort(key=lambda e: (e.ts, e.ts_raw, e.source_index))
    return dict(out)


def _nearest_distances(a: Sequence[Event], b: Sequence[Event]) -> List[float]:
    if not a or not b:
        return []
    bt = [e.ts.timestamp() for e in b]
    out: List[float] = []
    for e in a:
        t = e.ts.timestamp()
        i = bisect.bisect_left(bt, t)
        ds = []
        if i < len(bt):
            ds.append(abs(bt[i] - t))
        if i > 0:
            ds.append(abs(bt[i - 1] - t))
        if ds:
            out.append(min(ds))
    return out


def _pair_guardian_group(group: Sequence[Event], guardian_source: str,
                         by_source: Dict[str, List[Event]], stats: ParseStats,
                         args: argparse.Namespace) -> GuardianPair:
    """Pair one Guardian burst to a likely parent session.

    Explicit privacy-safe ID linkage wins. When the schema exposes no usable
    linkage, use reviewer state + temporal proximity and label the confidence.
    """
    if not group:
        return GuardianPair(guardian_source, None, "unpaired", "none")
    non_guardian = {
        src: evs for src, evs in by_source.items()
        if src != guardian_source and not any(e.is_auto_review_inference for e in evs)
    }
    if not non_guardian:
        return GuardianPair(guardian_source, None, "unpaired", "none")

    glink = stats.session_links.get(guardian_source, SessionLinkInfo())

    def temporal_rank(src: str) -> Tuple[float, float, float, float]:
        evs = non_guardian[src]
        ds = _nearest_distances(group, evs)
        if not ds:
            return (0.0, 0.0, float("inf"), float("inf"))
        cov60 = sum(d <= 60.0 for d in ds) / len(ds)
        covmatch = sum(d <= args.guardian_parent_match_seconds for d in ds) / len(ds)
        return (cov60, covmatch, statistics.median(ds), min(ds))

    explicit = []
    shared = []
    for src in non_guardian:
        plink = stats.session_links.get(src, SessionLinkInfo())
        if glink.parent_ids & plink.own_ids:
            explicit.append(src)
        elif glink.own_ids & plink.own_ids:
            shared.append(src)

    if explicit or shared:
        pool = explicit or shared
        ranked = sorted(pool, key=lambda src: (-temporal_rank(src)[0], -temporal_rank(src)[1],
                                               temporal_rank(src)[2], temporal_rank(src)[3], src))
        best = ranked[0]
        cov60, _covmatch, med, nearest = temporal_rank(best)
        ambiguous = len(ranked) > 1 and temporal_rank(ranked[1])[:2] == temporal_rank(best)[:2]
        method = "explicit-parent-id" if explicit else "shared-link-id"
        confidence = "high" if not ambiguous else "medium"
        return GuardianPair(guardian_source, best, confidence, method, nearest, cov60, ambiguous)

    # Prefer normal sessions that actually carried auto_review reviewer state.
    reviewer_pool = [
        src for src, evs in non_guardian.items()
        if any(e.approvals_reviewer == "auto_review" for e in evs)
    ]
    pool = reviewer_pool or list(non_guardian)
    scored = []
    for src in pool:
        cov60, covmatch, med, nearest = temporal_rank(src)
        if nearest <= args.guardian_parent_match_seconds:
            scored.append((src, cov60, covmatch, med, nearest))
    if not scored:
        return GuardianPair(guardian_source, None, "unpaired", "no-nearby-parent")
    scored.sort(key=lambda x: (-x[1], -x[2], x[3], x[4], x[0]))
    best = scored[0]
    ambiguous = False
    if len(scored) > 1:
        second = scored[1]
        ambiguous = (abs(best[1] - second[1]) < 0.10 and
                     abs(best[2] - second[2]) < 0.10 and
                     abs(best[4] - second[4]) <= 10.0)
    if ambiguous:
        confidence = "low"
    elif best[1] >= 0.50 or (best[4] <= 15.0 and best[1] > 0):
        confidence = "medium"
    else:
        confidence = "low"
    method = "temporal+reviewer" if reviewer_pool else "temporal"
    return GuardianPair(guardian_source, best[0], confidence, method, best[4], best[1], ambiguous)


def _split_event_groups(events: Sequence[Event], gap_seconds: float) -> List[List[Event]]:
    if not events:
        return []
    evs = sorted(events, key=lambda e: (e.ts, e.ts_raw, e.source_index))
    groups: List[List[Event]] = [[evs[0]]]
    for e in evs[1:]:
        if (e.ts - groups[-1][-1].ts).total_seconds() > gap_seconds:
            groups.append([e])
        else:
            groups[-1].append(e)
    return groups


def _dominant_value(events: Sequence[Event], attr: str) -> str:
    weights: Dict[str, int] = defaultdict(int)
    for e in events:
        value = str(getattr(e, attr, "unknown") or "unknown")
        weights[value] += max(e.tokens, 1)
    return max(weights.items(), key=lambda kv: kv[1])[0] if weights else "unknown"


def _build_quota_snapshot_indexes(events: Sequence[Event], args: argparse.Namespace) -> Dict[int, Dict[str, object]]:
    indexes: Dict[int, Dict[str, object]] = {}
    for minutes in discovered_windows(events):
        wevs = events_for_window(events, minutes)
        if not wevs:
            continue
        wevs = sorted(wevs, key=lambda e: (e.ts, e.ts_raw, e.source))
        times = [e.ts.timestamp() for e in wevs]
        raw_keys = [reset_key(e.reset_at, args.reset_tolerance) for e in wevs]
        highs: List[float] = []
        high_by_key: Dict[int, float] = {}
        for e, rk in zip(wevs, raw_keys):
            high_by_key[rk] = max(high_by_key.get(rk, e.used), e.used)
            highs.append(high_by_key[rk])
        indexes[minutes] = {"events": wevs, "times": times, "raw_keys": raw_keys, "highs": highs}
    return indexes


def _quota_envelope(index: Dict[str, object], start: datetime, end: datetime,
                    max_gap_seconds: float) -> Dict[str, object]:
    evs = index["events"]
    times = index["times"]
    raw_keys = index["raw_keys"]
    highs = index["highs"]
    assert isinstance(evs, list) and isinstance(times, list)
    st = start.timestamp()
    en = end.timestamp()
    before_i = bisect.bisect_left(times, st) - 1
    after_i = bisect.bisect_left(times, en)
    if before_i < 0 or after_i >= len(evs):
        return {"status": "missing-snapshot", "points": float("nan")}
    before = evs[before_i]
    after = evs[after_i]
    before_gap = st - times[before_i]
    after_gap = times[after_i] - en
    if before_gap > max_gap_seconds or after_gap > max_gap_seconds:
        return {"status": "stale-snapshot", "points": float("nan"),
                "before_gap_s": before_gap, "after_gap_s": after_gap}
    if raw_keys[before_i] != raw_keys[after_i]:
        return {"status": "reset-boundary", "points": float("nan"),
                "before_used": before.used, "after_used": after.used}
    points = max(0.0, float(highs[after_i]) - float(highs[before_i]))
    return {
        "status": "ok", "points": points,
        "before_used": before.used, "after_used": after.used,
        "before_high": float(highs[before_i]), "after_high": float(highs[after_i]),
        "before_gap_s": before_gap, "after_gap_s": after_gap,
    }


def _policy_regimes_for_approval_matching(primary: Analysis, args: argparse.Namespace) -> List[PolicyRegime]:
    rows = _dominant_weight_rows(primary.buckets, args.weight_model_purity)
    return detect_policy_regimes(
        rows, args.min_price_coverage, args.regime_min_episodes, args.regime_min_points,
        args.regime_min_ratio, args.regime_min_improvement,
    )


def _regime_label(regimes: Sequence[PolicyRegime], model: str, ts: datetime) -> str:
    for reg in regimes:
        if reg.model == model and reg.start_ts <= ts <= reg.end_ts:
            return reg.label
    return "unknown"


def build_approval_episodes(events: Sequence[Event], stats: ParseStats, args: argparse.Namespace,
                            primary: Optional[Analysis] = None) -> List[Dict[str, object]]:
    """Build Guardian approval episodes and pair them to likely parent work."""
    by_source = _source_events(events)
    quota_indexes = _build_quota_snapshot_indexes(events, args)
    regimes = _policy_regimes_for_approval_matching(primary, args) if primary is not None else []
    rows: List[Dict[str, object]] = []
    episode_id = 0

    for src, sev in sorted(by_source.items()):
        auto = [e for e in sev if e.is_auto_review_inference]
        if not auto:
            continue
        for group in _split_event_groups(auto, args.guardian_episode_gap_seconds):
            episode_id += 1
            pair = _pair_guardian_group(group, src, by_source, stats, args)
            parent_all = by_source.get(pair.parent_source or "", [])
            start, end = group[0].ts, group[-1].ts
            lo = start - timedelta(seconds=args.guardian_context_seconds)
            hi = end + timedelta(seconds=args.guardian_context_seconds)
            parent_context = [e for e in parent_all if lo <= e.ts <= hi and not e.is_auto_review_inference]
            parent_before = [e for e in parent_context if e.ts < start]
            parent_during = [e for e in parent_context if start <= e.ts <= end]
            parent_after = [e for e in parent_context if e.ts > end]
            model = _dominant_value(parent_context or parent_all, "model")
            effort = _dominant_value(parent_context or parent_all, "effort")
            unc, cached, out, reasoning = _event_token_totals(group)
            guardian_tokens = unc + cached + out
            parent_tokens = sum(e.tokens for e in parent_context)
            markers = []
            if pair.parent_source:
                markers = [m for m in stats.approval_markers.get(pair.parent_source, []) if lo <= m[0] <= hi]
            row: Dict[str, object] = {
                "episode_id": episode_id,
                "guardian_source": src,
                "parent_source": pair.parent_source or "",
                "pair_confidence": pair.confidence,
                "pair_method": pair.method,
                "pair_ambiguous": pair.ambiguous,
                "nearest_parent_seconds": pair.nearest_seconds,
                "parent_coverage_60s": pair.coverage_60s,
                "start": start, "end": end,
                "duration_seconds": max(0.0, (end - start).total_seconds()),
                "guardian_events": len(group),
                "guardian_tokens": guardian_tokens,
                "guardian_uncached": unc, "guardian_cached": cached, "guardian_output": out,
                "guardian_reasoning_output": reasoning,
                "parent_tokens_before": sum(e.tokens for e in parent_before),
                "parent_tokens_during": sum(e.tokens for e in parent_during),
                "parent_tokens_after": sum(e.tokens for e in parent_after),
                "parent_tokens": parent_tokens,
                "parent_events": len(parent_context),
                "parent_model": model, "parent_effort": effort,
                "policy_regime": _regime_label(regimes, model, start),
                "approval_markers_nearby": len(markers),
                "guardian_share_local": guardian_tokens / (guardian_tokens + parent_tokens)
                    if guardian_tokens + parent_tokens else float("nan"),
            }
            for minutes, idx in quota_indexes.items():
                q = _quota_envelope(idx, start, end, args.guardian_quota_snapshot_seconds)
                prefix = f"quota_{minutes}m_"
                row[prefix + "status"] = q.get("status", "unknown")
                row[prefix + "points"] = q.get("points", float("nan"))
                row[prefix + "before"] = q.get("before_high", q.get("before_used", float("nan")))
                row[prefix + "after"] = q.get("after_high", q.get("after_used", float("nan")))
            rows.append(row)
    return rows


def build_manual_approval_episodes(events: Sequence[Event], stats: ParseStats, args: argparse.Namespace,
                                   primary: Optional[Analysis] = None) -> List[Dict[str, object]]:
    """Build best-effort manual-approval controls from explicit approval markers."""
    by_source = _source_events(events)
    quota_indexes = _build_quota_snapshot_indexes(events, args)
    regimes = _policy_regimes_for_approval_matching(primary, args) if primary is not None else []
    rows: List[Dict[str, object]] = []
    mid = 0
    for src, markers in stats.approval_markers.items():
        sev = by_source.get(src, [])
        if not sev or not any(e.approvals_reviewer == "user" for e in sev):
            continue
        # Prefer request markers; if a schema only exposes generic approval markers, keep those.
        chosen = [m for m in markers if m[1] == "request"] or [m for m in markers if m[1] == "approval"]
        if not chosen:
            continue
        pseudo = []
        for ts, _kind in chosen:
            pseudo.append(Event(ts.isoformat(), ts, src, "unknown", 0, 0, 0, 0.0, 0.0,
                                args.window_minutes, False, None))
        for grp in _split_event_groups(pseudo, args.guardian_episode_gap_seconds):
            mid += 1
            start, end = grp[0].ts, grp[-1].ts
            lo = start - timedelta(seconds=args.guardian_context_seconds)
            hi = end + timedelta(seconds=args.guardian_context_seconds)
            ctx = [e for e in sev if lo <= e.ts <= hi and not e.is_auto_review_inference]
            if not ctx:
                continue
            model = _dominant_value(ctx, "model")
            effort = _dominant_value(ctx, "effort")
            row: Dict[str, object] = {
                "episode_id": mid, "source": src, "start": start, "end": end,
                "parent_tokens": sum(e.tokens for e in ctx), "parent_events": len(ctx),
                "parent_model": model, "parent_effort": effort,
                "policy_regime": _regime_label(regimes, model, start),
            }
            for minutes, idx in quota_indexes.items():
                q = _quota_envelope(idx, start, end, args.guardian_quota_snapshot_seconds)
                prefix = f"quota_{minutes}m_"
                row[prefix + "status"] = q.get("status", "unknown")
                row[prefix + "points"] = q.get("points", float("nan"))
            rows.append(row)
    return rows


def _matched_manual_pairs(auto_rows: Sequence[Dict[str, object]], manual_rows: Sequence[Dict[str, object]],
                          minutes: int, token_ratio: float) -> List[Tuple[Dict[str, object], Dict[str, object]]]:
    usable_manual = [r for r in manual_rows if r.get(f"quota_{minutes}m_status") == "ok"]
    used = set()
    pairs = []
    for a in auto_rows:
        if a.get("pair_confidence") not in {"high", "medium"}:
            continue
        if a.get(f"quota_{minutes}m_status") != "ok":
            continue
        atok = max(float(a.get("parent_tokens", 0) or 0), 1.0)
        candidates = []
        for i, m in enumerate(usable_manual):
            if i in used:
                continue
            if (m.get("parent_model") != a.get("parent_model") or
                    m.get("parent_effort") != a.get("parent_effort") or
                    m.get("policy_regime") != a.get("policy_regime") or
                    m.get("policy_regime") == "unknown"):
                continue
            mtok = max(float(m.get("parent_tokens", 0) or 0), 1.0)
            ratio = max(atok / mtok, mtok / atok)
            if ratio > token_ratio:
                continue
            dt = abs((m["start"] - a["start"]).total_seconds())
            candidates.append((abs(math.log(atok / mtok)), dt, i, m))
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]))
            _lr, _dt, i, m = candidates[0]
            used.add(i)
            pairs.append((a, m))
    return pairs


def _nnls_two_feature(rows: Sequence[Tuple[float, float, float]]) -> Tuple[float, float, float, float]:
    """Tiny non-negative least-squares solver for y ~= b1*x1 + b2*x2."""
    if not rows:
        return 0.0, 0.0, float("inf"), float("inf")
    a = sum(x1*x1 for x1, _x2, _y in rows)
    b = sum(x1*x2 for x1, x2, _y in rows)
    c = sum(x2*x2 for _x1, x2, _y in rows)
    d = sum(x1*y for x1, _x2, y in rows)
    e = sum(x2*y for _x1, x2, y in rows)
    candidates = [(0.0, 0.0)]
    if a > EPS:
        candidates.append((max(0.0, d/a), 0.0))
    if c > EPS:
        candidates.append((0.0, max(0.0, e/c)))
    det = a*c - b*b
    if det > EPS:
        b1 = (d*c - e*b) / det
        b2 = (e*a - d*b) / det
        if b1 >= 0 and b2 >= 0:
            candidates.append((b1, b2))
    def sse(beta: Tuple[float, float]) -> float:
        return sum((y - beta[0]*x1 - beta[1]*x2)**2 for x1, x2, y in rows)
    best = min(candidates, key=sse)
    parent_only = sse((max(0.0, d/a) if a > EPS else 0.0, 0.0))
    rho = b / math.sqrt(a*c) if a > EPS and c > EPS else 1.0
    rho = min(max(rho, 0.0), 0.999999999)
    condition = math.sqrt((1.0 + rho) / max(1e-12, 1.0 - rho))
    improve = 1.0 - sse(best) / parent_only if parent_only > EPS else 0.0
    return best[0], best[1], condition, improve


def guardian_incremental_fit(episodes: Sequence[Dict[str, object]], minutes: int,
                             boots: int) -> Dict[str, object]:
    rows = []
    groups: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    for r in episodes:
        if r.get("pair_confidence") not in {"high", "medium"}:
            continue
        if r.get(f"quota_{minutes}m_status") != "ok":
            continue
        parent = float(r.get("parent_tokens", 0) or 0) / 1e6
        guardian = float(r.get("guardian_tokens", 0) or 0) / 1e6
        points = float(r.get(f"quota_{minutes}m_points", 0) or 0)
        if parent <= 0 or guardian <= 0:
            continue
        row = (parent, guardian, points)
        rows.append(row)
        groups[str(r.get("parent_source", ""))].append(row)
    if len(rows) < 30 or len(groups) < 5 or sum(r[2] for r in rows) < 10:
        return {"status": "not identifiable", "reason": "insufficient independent approval episodes",
                "episodes": len(rows), "sessions": len(groups)}
    bp, bg, cond, improve = _nnls_two_feature(rows)
    if cond > 20.0:
        return {"status": "not identifiable", "reason": f"parent/Guardian workload is too collinear (condition={cond:.1f})",
                "episodes": len(rows), "sessions": len(groups), "condition": cond}
    if bg <= EPS or improve < 0.05:
        return {"status": "not identifiable", "reason": "Guardian term adds too little stable explanatory value",
                "episodes": len(rows), "sessions": len(groups), "condition": cond, "improvement": improve}
    rng = random.Random(_stable_seed("guardian-fit", minutes, len(rows), len(groups)))
    keys = sorted(groups)
    gb = []
    for _ in range(max(0, boots)):
        sample = []
        for _j in range(len(keys)):
            k = rng.choice(keys)
            sample.extend(groups[k])
        _p, g, _c, _i = _nnls_two_feature(sample)
        gb.append(g)
    if len(gb) < 50:
        return {"status": "not identifiable", "reason": "too few bootstrap replicates",
                "episodes": len(rows), "sessions": len(groups)}
    lo, med, hi = _q(gb, .10), _q(gb, .50), _q(gb, .90)
    positive = sum(x > EPS for x in gb) / len(gb)
    if lo <= EPS or positive < .80 or (lo > EPS and hi / lo > 10.0):
        return {"status": "not identifiable", "reason": "Guardian coefficient is unstable across parent-session bootstrap resamples",
                "episodes": len(rows), "sessions": len(groups), "condition": cond,
                "bootstrap_positive": positive}
    return {"status": "supported", "episodes": len(rows), "sessions": len(groups),
            "parent_coef": bp, "guardian_coef": bg, "guardian_lo": lo, "guardian_med": med,
            "guardian_hi": hi, "condition": cond, "improvement": improve}


def guardian_bucket_rows(events: Sequence[Event], args: argparse.Namespace) -> List[Dict[str, object]]:
    """Build conservative attribution rows for every requested/discovered limit window.

    A quota point is never called "caused by Guardian" merely because an auto-review
    record observed it. Rows distinguish auto-review-present, dominant, exclusive,
    and locally isolated buckets. Local isolation means no non-auto-review token event
    was logged around the bucket; account-global work outside these logs can still exist.
    """
    rows: List[Dict[str, object]] = []
    available = set(discovered_windows(events))
    wanted = [w for w in DEFAULT_GUARDIAN_WINDOWS if w in available]
    # Also retain unexpected/future windows rather than silently discarding them.
    wanted += [w for w in sorted(available) if w not in wanted]
    non_auto_times = _non_auto_review_timestamps(events)

    for minutes in wanted:
        wevs = events_for_window(events, minutes)
        if not wevs:
            continue
        wa = argparse.Namespace(**vars(args))
        wa.window_minutes = minutes
        analysis = analyze_events(wevs, wa)
        for b in analysis.buckets:
            auto_tokens = b.activity_tokens.get("auto-review inference", 0)
            auto_parent_tokens = b.activity_tokens.get("auto-review parent", 0)
            user_parent_tokens = b.activity_tokens.get("user-review parent", 0)
            other_tokens = max(b.total_tokens - auto_tokens, 0)
            share = auto_tokens / b.total_tokens if b.total_tokens else 0.0
            present = auto_tokens > 0
            exclusive = present and other_tokens == 0
            dominant = present and share + EPS >= args.guardian_purity
            isolated = (
                exclusive
                and not _has_nearby_non_auto_review(
                    non_auto_times, b.start_ts, b.end_ts, args.guardian_isolation_seconds
                )
            )
            rows.append({
                "window_minutes": minutes,
                "window": window_label(minutes),
                "reset_key": b.reset_key,
                "start": b.start_ts,
                "end": b.end_ts,
                "points": b.points,
                "events": b.events,
                "total_tokens": b.total_tokens,
                "auto_review_tokens": auto_tokens,
                "auto_review_share": share,
                "auto_review_present": present,
                "auto_review_dominant": dominant,
                "auto_review_exclusive": exclusive,
                "locally_isolated": isolated,
                "auto_review_parent_tokens": auto_parent_tokens,
                "user_review_parent_tokens": user_parent_tokens,
            })
    return rows


def print_guardian_audit(events: Sequence[Event], args: argparse.Namespace,
                         rows: Optional[Sequence[Dict[str, object]]] = None,
                         stats: Optional[ParseStats] = None,
                         approval_episodes: Optional[Sequence[Dict[str, object]]] = None,
                         primary: Optional[Analysis] = None) -> None:
    print("\
Guardian / auto-review approval audit")
    print("-------------------------------------")
    auto = [e for e in events if e.is_auto_review_inference]
    confirmed = [e for e in auto if e.is_confirmed_guardian]
    parent_auto = [e for e in events if not e.is_auto_review_inference
                   and e.approvals_reviewer == "auto_review"]
    parent_user = [e for e in events if not e.is_auto_review_inference
                   and e.approvals_reviewer == "user"]
    stats = stats or ParseStats()

    print("Guardian is treated as extra inference spawned by normal work, not as unrelated")
    print("background activity. Parent + Guardian are grouped into approval episodes; quota")
    print("movement remains account-global and is therefore reported as an observed envelope.\
")

    if not auto:
        print("No codex-auto-review inference events were found after replay filtering.")
    else:
        unc, cached, out, reasoning = _event_token_totals(auto)
        total = unc + cached + out
        print(f"auto-review inference sessions:     {_session_count(auto):,}")
        print(f"confirmed subagent sessions:        {_session_count(confirmed):,}")
        print(f"auto-review inference events:       {len(auto):,}")
        print(f"auto-review tokens:                 {total / 1e6:,.1f}M")
        print(f"  uncached input:                   {unc / 1e6:,.1f}M")
        print(f"  cached input:                     {cached / 1e6:,.1f}M")
        print(f"  output:                           {out / 1e6:,.1f}M")
        print(f"  reasoning output (subset/field):  {reasoning / 1e6:,.1f}M")
        print(f"  cached share of input:            {fmt_pct_ratio(_cache_ratio_events(auto))}")

    if approval_episodes is None:
        approval_episodes = build_approval_episodes(events, stats, args, primary)
    episodes = list(approval_episodes)
    print("\
Parent ↔ Guardian pairing and approval episodes")
    print("-----------------------------------------------")
    if not episodes:
        print("No Guardian approval episodes could be constructed.")
    else:
        counts = Counter(str(r.get("pair_confidence", "unpaired")) for r in episodes)
        methods = Counter(str(r.get("pair_method", "none")) for r in episodes)
        paired = [r for r in episodes if r.get("parent_source")]
        good = [r for r in episodes if r.get("pair_confidence") in {"high", "medium"}]
        marker_hits = sum(int(r.get("approval_markers_nearby", 0) or 0) > 0 for r in episodes)
        print(f"approval episodes:                   {len(episodes):,}")
        print(f"paired to a parent session:          {len(paired):,}")
        print(f"  high confidence:                   {counts.get('high', 0):,}")
        print(f"  medium confidence:                 {counts.get('medium', 0):,}")
        print(f"  low confidence:                    {counts.get('low', 0):,}")
        print(f"  unpaired:                          {counts.get('unpaired', 0):,}")
        print(f"episodes with explicit approval marker nearby: {marker_hits:,}")
        explicit_n = sum(v for k, v in methods.items() if k in {"explicit-parent-id", "shared-link-id"})
        print(f"episodes paired via discovered linkage IDs:    {explicit_n:,}")
        if good:
            gt = [float(r["guardian_tokens"]) for r in good]
            shares = [float(r["guardian_share_local"]) for r in good
                      if float(r.get("guardian_share_local", float('nan'))) == float(r.get("guardian_share_local", float('nan')))]
            print(f"Guardian tokens / approval p10/p50/p90: "
                  f"{_q(gt,.10)/1e6:.2f}M / {_q(gt,.50)/1e6:.2f}M / {_q(gt,.90)/1e6:.2f}M")
            if shares:
                print(f"Guardian share of local parent+review context, median: {_q(shares,.50):.0%}")
            print("Pairing uses explicit hashed linkage metadata when available; otherwise temporal")
            print("proximity and reviewer state are used and confidence is downgraded accordingly.")

    print("\
Reviewer setting observed on non-auto-review work")
    print("-------------------------------------------------")
    print(f"{'reviewer':<14} {'sessions':>9} {'events':>10} {'tokens M':>11} {'cache':>7}")
    for name, evs in (("auto_review", parent_auto), ("user", parent_user)):
        if not evs:
            print(f"{name:<14} {0:>9} {0:>10} {0:>11} {'n/a':>7}")
            continue
        print(f"{name:<14} {_session_count(evs):>9,} {len(evs):>10,} "
              f"{sum(e.tokens for e in evs)/1e6:>11.1f} {fmt_pct_ratio(_cache_ratio_events(evs)):>7}")
    print("These rows describe parent workload under each reviewer setting; Guardian inference")
    print("itself is kept separate above.")

    available = discovered_windows(events)
    print("\
Approval-episode quota envelopes")
    print("--------------------------------")
    if available:
        print("discovered windows: " + ", ".join(f"{window_label(w)} ({w}m)" for w in available))
    print(f"{'limit':<7} {'usable ep':>9} {'obs pt':>8} {'Guardian M':>11} {'parent M':>10} {'G M/obs pt':>11}")
    for minutes in available:
        usable = [r for r in episodes if r.get("pair_confidence") in {"high", "medium"}
                  and r.get(f"quota_{minutes}m_status") == "ok"]
        pts = sum(float(r.get(f"quota_{minutes}m_points", 0) or 0) for r in usable)
        gt = sum(int(r.get("guardian_tokens", 0) or 0) for r in usable)
        pt = sum(int(r.get("parent_tokens", 0) or 0) for r in usable)
        ratio = gt / 1e6 / pts if pts > EPS else float("nan")
        print(f"{window_label(minutes):<7} {len(usable):>9,} {pts:>8.0f} {gt/1e6:>11.1f} {pt/1e6:>10.1f} "
              f"{(f'{ratio:.2f}' if ratio == ratio else 'n/a'):>11}")
        if auto and not any(e.is_auto_review_inference and minutes in e.rate_windows for e in events):
            print(f"        note: no codex-auto-review events overlap retained {window_label(minutes)} telemetry.")
    print("Observed points are the account-global high-water change between fresh snapshots")
    print("around the approval episode. They include parent work and any other account activity;")
    print("they are not quota points attributed solely to Guardian.")

    # Best-effort manual approval controls from explicit request markers.
    manual = build_manual_approval_episodes(events, stats, args, primary)
    print("\
Matched manual-approval comparison")
    print("----------------------------------")
    if not manual:
        print("No explicit user-approval request markers were detected in usable user-review sessions.")
        print("Matched causal comparison is unavailable; reviewer-mode aggregates are shown below instead.")
    else:
        print(f"manual approval episodes detected: {len(manual):,}")
        for minutes in available:
            pairs = _matched_manual_pairs(episodes, manual, minutes, args.guardian_match_token_ratio)
            if not pairs:
                continue
            apts = sum(float(a.get(f"quota_{minutes}m_points", 0) or 0) for a, _m in pairs)
            mpts = sum(float(m.get(f"quota_{minutes}m_points", 0) or 0) for _a, m in pairs)
            aparent = sum(int(a.get("parent_tokens", 0) or 0) for a, _m in pairs)
            mparent = sum(int(m.get("parent_tokens", 0) or 0) for _a, m in pairs)
            guardian = sum(int(a.get("guardian_tokens", 0) or 0) for a, _m in pairs)
            print(f"{window_label(minutes)}: {len(pairs)} matched pairs, "
                  f"auto episodes {apts:.0f} observed pt on {(aparent+guardian)/1e6:.1f}M local tokens; "
                  f"manual {mpts:.0f} pt on {mparent/1e6:.1f}M")
        print("Matches require the same parent model, reasoning effort, detected policy regime,")
        print(f"and parent-token workload within {args.guardian_match_token_ratio:g}x. Results remain observational.")

    print("\
Incremental Guardian quota fit (exploratory)")
    print("--------------------------------------------")
    for minutes in available:
        fit = guardian_incremental_fit(episodes, minutes, args.guardian_fit_bootstraps)
        if fit.get("status") != "supported":
            if fit.get("episodes", 0):
                print(f"{window_label(minutes)}: not identifiable ({fit.get('reason')}; "
                      f"{fit.get('episodes')} episodes / {fit.get('sessions')} parent sessions)")
            continue
        g = float(fit["guardian_coef"])
        lo, med, hi = float(fit["guardian_lo"]), float(fit["guardian_med"]), float(fit["guardian_hi"])
        inv = 1.0 / g if g > EPS else float("nan")
        print(f"{window_label(minutes)}: provisional Guardian coefficient {g:.3f} quota pt/Mtok "
              f"(bootstrap median {med:.3f}, 80% {lo:.3f}-{hi:.3f}); ~{inv:.2f}M Guardian tok/pt")
        print("        This is an account-global observational fit, not an internal OpenAI quota formula.")

    if rows is None:
        rows = guardian_bucket_rows(events, args)
    print("\
Reviewer-mode quota buckets (descriptive, not causal)")
    print("---------------------------------------------------")
    print(f"Buckets require >= {args.guardian_purity:.0%} raw-token purity for the reviewer mode and")
    print("exclude buckets containing codex-auto-review inference. Model/time/policy mix can still confound them.")
    print(f"{'limit':<7} {'reviewer':<12} {'pt':>7} {'buckets':>8} {'tokens M':>10} {'Mtok/pt':>9}")
    for minutes in available:
        wr = [r for r in rows if int(r["window_minutes"]) == minutes]
        for reviewer, token_key in (("auto_review", "auto_review_parent_tokens"),
                                    ("user", "user_review_parent_tokens")):
            pure = []
            for r in wr:
                total = int(r["total_tokens"])
                mode_tokens = int(r[token_key])
                auto_tokens = int(r["auto_review_tokens"])
                share = mode_tokens / total if total else 0.0
                if auto_tokens == 0 and mode_tokens > 0 and share + EPS >= args.guardian_purity:
                    pure.append(r)
            pts = sum(float(r["points"]) for r in pure)
            toks = sum(int(r[token_key]) for r in pure)
            mtok_pt = toks / 1e6 / pts if pts > EPS else float("nan")
            print(f"{window_label(minutes):<7} {reviewer:<12} {pts:>7.0f} {len(pure):>8,} "
                  f"{toks/1e6:>10.1f} {(f'{mtok_pt:.2f}' if mtok_pt == mtok_pt else 'n/a'):>9}")

def export_guardian_buckets_csv(path: str, rows: Sequence[Dict[str, object]]) -> None:
    fields = [
        "window_minutes", "window", "reset_key", "start", "end", "points", "events",
        "total_tokens", "auto_review_tokens", "auto_review_share", "auto_review_present",
        "auto_review_dominant", "auto_review_exclusive", "locally_isolated",
        "auto_review_parent_tokens", "user_review_parent_tokens",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            cooked = dict(row)
            for key in ("start", "end"):
                value = cooked.get(key)
                if isinstance(value, datetime):
                    cooked[key] = value.isoformat()
            w.writerow({k: cooked.get(k, "") for k in fields})


def export_approval_episodes_csv(path: str, rows: Sequence[Dict[str, object]]) -> None:
    base = [
        "episode_id", "start", "end", "duration_seconds", "pair_confidence", "pair_method",
        "pair_ambiguous", "nearest_parent_seconds", "parent_coverage_60s", "guardian_events",
        "guardian_tokens", "guardian_uncached", "guardian_cached", "guardian_output",
        "guardian_reasoning_output", "parent_tokens_before", "parent_tokens_during",
        "parent_tokens_after", "parent_tokens", "parent_events", "parent_model", "parent_effort",
        "policy_regime", "approval_markers_nearby", "guardian_share_local",
    ]
    quota_fields = sorted({k for r in rows for k in r if k.startswith("quota_")})
    fields = base + quota_fields
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            cooked = dict(row)
            for key in ("start", "end"):
                if isinstance(cooked.get(key), datetime):
                    cooked[key] = cooked[key].isoformat()
            # File paths/opaque session IDs are intentionally omitted.
            w.writerow({k: cooked.get(k, "") for k in fields})


def print_unpriced(events: Sequence[Event], prices: Dict[str, Tuple[float, float, float]]) -> None:
    missing: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for e in events:
        if e.model not in prices:
            missing[e.model][0] += 1
            missing[e.model][1] += e.tokens
    if not missing:
        return
    print("\nUnpriced models")
    print("---------------")
    print("API-normalized metrics exclude buckets that fail the pricing coverage threshold.")
    print(f"{'model':<28} {'turns':>9} {'tokens M':>10}")
    for model, (turns, toks) in sorted(missing.items(), key=lambda kv: -kv[1][1]):
        print(f"{model:<28} {turns:9d} {toks/1e6:10.1f}")


def print_notes(analysis: Analysis) -> None:
    backsteps = sum(ep.backstep_observations for ep in analysis.episodes)
    churn = sum(r.kind == "churn" for r in analysis.ledger)
    print("\nInterpretation notes")
    print("--------------------")
    print("* resets_at changes are not automatically called resets. Near-zero no-op changes")
    print("  are merged as churn; scheduled/material-drop changes are effective resets.")
    print("* Ambiguous resets_at transitions still start a fresh high-water accounting episode")
    print("  so a possible real reset cannot cause quota usage to be under-counted.")
    print("* Within an accounting episode, used_percent is high-watered. Lower readings are")
    print("  treated as telemetry backsteps/stale observations, not quota replenishment.")
    print("* API$/pt uses only high-water buckets meeting the configured pricing coverage.")
    print("* API pricing is a normalization yardstick, not internal cost or plan billing.")
    if churn:
        print(f"* Near-zero resets_at churn transitions merged: {churn:,}.")
    if backsteps:
        print(f"* Same-episode below-high-water readings observed: {backsteps:,}.")
    if not analysis.buckets:
        print("* No attributable high-water buckets were found.")


# ---------------------------------------------------------------------------
# Model x effort chart aggregation (v2.6)
# ---------------------------------------------------------------------------

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "ultra", "max")


def _stable_seed(*parts: object) -> int:
    text = "|".join(str(x) for x in parts)
    return 7 + sum((i + 1) * ord(ch) for i, ch in enumerate(text)) % 2_000_000_000


def _metric_from_buckets(bs: Sequence[Bucket], min_price_coverage: float) -> Dict[str, float]:
    pts = sum(b.points for b in bs)
    toks = sum(b.total_tokens for b in bs)
    unc = sum(b.usage.uncached for b in bs)
    cached = sum(b.usage.cached for b in bs)
    out = sum(b.usage.output for b in bs)
    priced = [b for b in bs if usable_bucket(b, min_price_coverage)]
    api_pts = sum(b.points for b in priced)
    api_usd = sum(b.usage.api_usd or 0.0 for b in priced)
    return {
        "points": pts,
        "tokens_m_per_point": toks / 1e6 / pts if pts > EPS else float("nan"),
        "uncached_m_per_point": unc / 1e6 / pts if pts > EPS else float("nan"),
        "cached_m_per_point": cached / 1e6 / pts if pts > EPS else float("nan"),
        "output_m_per_point": out / 1e6 / pts if pts > EPS else float("nan"),
        "api_points": api_pts,
        "api_usd": api_usd,
        "api_usd_per_point": api_usd / api_pts if api_pts > EPS else float("nan"),
    }


def _episode_bootstrap_metrics(bs: Sequence[Bucket], min_price_coverage: float,
                               boots: int, interval: float, seed: int) -> Dict[str, float]:
    by_ep: Dict[int, List[Bucket]] = defaultdict(list)
    for b in bs:
        by_ep[b.reset_key].append(b)
    ids = sorted(by_ep)
    if len(ids) < 2 or boots <= 0:
        return {
            "tokens_lo": float("nan"), "tokens_hi": float("nan"),
            "api_lo": float("nan"), "api_hi": float("nan"),
        }
    rng = random.Random(seed)
    token_vals: List[float] = []
    api_vals: List[float] = []
    for _ in range(boots):
        sample: List[Bucket] = []
        for _j in ids:
            sample.extend(by_ep[rng.choice(ids)])
        m = _metric_from_buckets(sample, min_price_coverage)
        if m["tokens_m_per_point"] == m["tokens_m_per_point"]:
            token_vals.append(m["tokens_m_per_point"])
        if m["api_usd_per_point"] == m["api_usd_per_point"]:
            api_vals.append(m["api_usd_per_point"])
    tail = max(0.0, min(0.5, (1.0 - interval) / 2.0))
    return {
        "tokens_lo": _q(token_vals, tail),
        "tokens_hi": _q(token_vals, 1.0 - tail),
        "api_lo": _q(api_vals, tail),
        "api_hi": _q(api_vals, 1.0 - tail),
    }


def build_chart_rows(buckets: Sequence[Bucket], args: argparse.Namespace) -> List[Dict[str, object]]:
    """Aggregate model x effort chart rows inside detected model policy regimes."""
    model_rows = _dominant_weight_rows(buckets, args.chart_model_purity)
    regimes = detect_policy_regimes(
        model_rows, args.min_price_coverage, args.regime_min_episodes,
        args.regime_min_points, args.regime_min_ratio, args.regime_min_improvement,
    )
    if not regimes:
        return []

    latest: Dict[str, PolicyRegime] = {}
    for rg in regimes:
        if rg.model not in latest or rg.end_ts > latest[rg.model].end_ts:
            latest[rg.model] = rg

    groups: Dict[Tuple[str, int, str], List[Bucket]] = defaultdict(list)
    regime_by_key: Dict[Tuple[str, int], PolicyRegime] = {}
    for rg in regimes:
        if not args.chart_all_regimes and latest.get(rg.model) is not rg:
            continue
        regime_by_key[(rg.model, rg.index)] = rg
        episode_ids = set(rg.episode_ids)
        for b in buckets:
            if b.reset_key not in episode_ids or b.points <= EPS:
                continue
            model, model_share = b.dominant_model
            effort, effort_share = b.dominant_effort
            if model != rg.model or model_share + EPS < args.chart_model_purity:
                continue
            if effort == "unknown" or effort_share + EPS < args.chart_effort_purity:
                continue
            groups[(model, rg.index, effort)].append(b)

    rows: List[Dict[str, object]] = []
    for (model, idx, effort), bs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], EFFORT_ORDER.index(kv[0][2]) if kv[0][2] in EFFORT_ORDER else 99, kv[0][2])):
        rg = regime_by_key[(model, idx)]
        pts = sum(b.points for b in bs)
        eps = len({b.reset_key for b in bs})
        if pts + EPS < args.chart_min_points or eps < args.chart_min_episodes:
            continue
        m = _metric_from_buckets(bs, args.min_price_coverage)
        api_eps = len({b.reset_key for b in bs if usable_bucket(b, args.min_price_coverage)})
        model_tokens = sum(b.model_tokens.get(model, 0) for b in bs)
        effort_tokens = sum(b.effort_tokens.get(effort, 0) for b in bs)
        total_tokens = sum(b.total_tokens for b in bs)
        ci = _episode_bootstrap_metrics(
            bs, args.min_price_coverage, args.chart_bootstraps, args.chart_interval,
            _stable_seed(model, idx, effort),
        )
        api_supported = m["api_points"] + EPS >= args.chart_min_points and api_eps >= args.chart_min_episodes
        rows.append({
            "model": model,
            "regime": rg.label,
            "regime_start": rg.start_ts.isoformat(),
            "regime_end": rg.end_ts.isoformat(),
            "regime_detection_basis": rg.detection_basis,
            "effort": effort,
            "quota_points": pts,
            "episodes": eps,
            "buckets": len(bs),
            "model_token_share": model_tokens / total_tokens if total_tokens else float("nan"),
            "effort_token_share": effort_tokens / total_tokens if total_tokens else float("nan"),
            "tokens_m_per_point": m["tokens_m_per_point"],
            "tokens_p10_m": ci["tokens_lo"],
            "tokens_p90_m": ci["tokens_hi"],
            "uncached_m_per_point": m["uncached_m_per_point"],
            "cached_m_per_point": m["cached_m_per_point"],
            "output_m_per_point": m["output_m_per_point"],
            "api_quota_points": m["api_points"],
            "api_episodes": api_eps,
            "api_usd_per_point": m["api_usd_per_point"] if api_supported else float("nan"),
            "api_p10": ci["api_lo"] if api_supported else float("nan"),
            "api_p90": ci["api_hi"] if api_supported else float("nan"),
        })
    return rows


def export_chart_data_csv(path: str, rows: Sequence[Dict[str, object]]) -> None:
    fields = [
        "model", "regime", "regime_start", "regime_end", "regime_detection_basis", "effort",
        "quota_points", "episodes", "buckets", "model_token_share", "effort_token_share",
        "tokens_m_per_point", "tokens_p10_m", "tokens_p90_m",
        "uncached_m_per_point", "cached_m_per_point", "output_m_per_point",
        "api_quota_points", "api_episodes", "api_usd_per_point", "api_p10", "api_p90",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            cleaned = {}
            for key in fields:
                value = row.get(key, "")
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    value = ""
                cleaned[key] = value
            w.writerow(cleaned)


def print_chart_data_summary(rows: Sequence[Dict[str, object]], args: argparse.Namespace) -> None:
    print("\nChart data")
    print("----------")
    if not rows:
        print("No model x effort combinations meet the chart purity/sample thresholds.")
        return
    scope = "all detected policy regimes" if args.chart_all_regimes else "latest detected policy regime per model"
    print(f"scope:                         {scope}")
    print(f"model / effort purity:         {args.chart_model_purity:.0%} / {args.chart_effort_purity:.0%}")
    print(f"minimum evidence:              {args.chart_min_points:g} quota points, {args.chart_min_episodes} episodes")
    print(f"bootstrap interval:            {args.chart_interval:.0%} from {args.chart_bootstraps} whole-episode resamples")
    print(f"chart rows:                    {len(rows):,}")


EFFORT_COLORS = {
    "low": "#57d68d",
    "medium": "#43b7e9",
    "high": "#f5a316",
    "xhigh": "#f06d88",
    "ultra": "#b87bea",
    "max": "#d9dde5",
}
CHART_BG = "#081321"
CHART_AX_BG = "#111d31"
CHART_FG = "#dce4ee"
CHART_MUTED = "#93a4ba"
CHART_GRID = "#29364b"


def render_quota_chart(rows: Sequence[Dict[str, object]], prefix: str,
                       interval: float, all_regimes: bool,
                       title: str = "Quota value by model and effort") -> Tuple[str, str]:
    """Write the model x effort chart as PNG and SVG.

    matplotlib is intentionally imported lazily so the normal analyzer remains
    standard-library-only.
    """
    if not rows:
        raise RuntimeError("no qualifying model x effort chart rows")
    try:
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
    except ImportError as exc:
        raise RuntimeError(
            "Charts require matplotlib.\n\n"
            "Create a virtual environment and install it with:\n"
            "  python3 -m venv .venv\n"
            "  source .venv/bin/activate\n"
            "  python3 -m pip install matplotlib\n\n"
            "Then run:\n"
            "  python3 codex_quota_audit.py --charts"
        ) from exc

    regimes_per_model: Dict[str, set] = defaultdict(set)
    for r in rows:
        regimes_per_model[str(r["model"])].add(str(r["regime"]))
    multi_regime_models = {m for m, regs in regimes_per_model.items() if len(regs) > 1}

    groups = sorted(
        {(str(r["model"]), str(r["regime"]), str(r.get("regime_start", ""))) for r in rows},
        key=lambda x: (x[0], x[2], x[1]),
    )
    group_index = {(m, rg): i for i, (m, rg, _s) in enumerate(groups)}
    group_rows: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    for r in rows:
        group_rows[(str(r["model"]), str(r["regime"]))].append(r)

    n_groups = len(groups)
    fig_h = max(6.7, 1.25 * n_groups + 2.9)
    fig, axes = plt.subplots(1, 2, figsize=(17.2, fig_h), sharey=False)
    fig.patch.set_facecolor(CHART_BG)

    for ax in axes:
        ax.set_facecolor(CHART_AX_BG)
        for spine in ax.spines.values():
            spine.set_color(CHART_GRID)
        ax.tick_params(colors=CHART_MUTED, labelsize=10)
        ax.xaxis.grid(True, color=CHART_GRID, linewidth=0.8)
        ax.yaxis.grid(False)
        ax.set_axisbelow(True)

    present_efforts = [e for e in EFFORT_ORDER if any(str(r["effort"]) == e for r in rows)]
    extras = sorted({str(r["effort"]) for r in rows} - set(EFFORT_ORDER))
    present_efforts += extras
    offsets: Dict[str, float] = {}
    if len(present_efforts) == 1:
        offsets[present_efforts[0]] = 0.0
    else:
        span = 0.42
        for i, effort in enumerate(present_efforts):
            offsets[effort] = -span / 2 + span * i / (len(present_efforts) - 1)

    def finite(x: object) -> bool:
        try:
            return math.isfinite(float(x))
        except (TypeError, ValueError):
            return False

    token_candidates = [
        float(r["tokens_p90_m"]) if finite(r.get("tokens_p90_m")) else float(r["tokens_m_per_point"])
        for r in rows if finite(r.get("tokens_m_per_point"))
    ]
    api_candidates = [
        float(r["api_p90"]) if finite(r.get("api_p90")) else float(r["api_usd_per_point"])
        for r in rows if finite(r.get("api_usd_per_point"))
    ]
    token_xlim = max(1.0, max(token_candidates or [1.0]) * 1.22)
    api_xlim = max(1.0, max(api_candidates or [1.0]) * 1.22)

    panels = [
        (axes[0], "tokens_m_per_point", "tokens_p10_m", "tokens_p90_m", token_xlim),
        (axes[1], "api_usd_per_point", "api_p10", "api_p90", api_xlim),
    ]
    for panel, (ax, value_key, lo_key, hi_key, xmax) in enumerate(panels):
        for r in rows:
            model, regime, effort = str(r["model"]), str(r["regime"]), str(r["effort"])
            y = n_groups - 1 - group_index[(model, regime)] + offsets.get(effort, 0.0)
            if not finite(r.get(value_key)):
                continue
            val = float(r[value_key])
            lo = float(r[lo_key]) if finite(r.get(lo_key)) else val
            hi = float(r[hi_key]) if finite(r.get(hi_key)) else val
            color = EFFORT_COLORS.get(effort, "#c8d0da")
            ax.errorbar(
                val, y,
                xerr=[[max(0.0, val - lo)], [max(0.0, hi - val)]],
                fmt="o", markersize=7.5, color=color, ecolor=color,
                elinewidth=1.8, capsize=4, capthick=1.4, zorder=3,
            )
            pts_key = "quota_points" if panel == 0 else "api_quota_points"
            eps_key = "episodes" if panel == 0 else "api_episodes"
            pts = float(r.get(pts_key, 0) or 0)
            eps = float(r.get(eps_key, 0) or 0)
            text_x = min(xmax * 0.97, max(val, hi) + xmax * 0.012)
            ax.text(text_x, y, f"{val:.1f} · {pts:.0f}pt · {eps:.0f}ep",
                    color=CHART_FG, fontsize=8.4, va="center", ha="left")

        if panel == 1:
            for model, regime, _start in groups:
                rs = group_rows[(model, regime)]
                if not any(finite(r.get("api_usd_per_point")) for r in rs):
                    y0 = n_groups - 1 - group_index[(model, regime)]
                    ax.text(xmax * 0.42, y0, "unpriced / insufficient priced data",
                            color=CHART_MUTED, fontsize=8.5, fontstyle="italic",
                            va="center", ha="center")

        labels, yticks = [], []
        for model, regime, _start in groups:
            yticks.append(n_groups - 1 - group_index[(model, regime)])
            labels.append(f"{model} {regime}" if model in multi_regime_models else model)
        ax.set_yticks(yticks)
        ax.set_yticklabels(labels, color=CHART_FG, fontsize=11, fontweight="bold")
        ax.set_ylim(-0.65, n_groups - 0.35)
        ax.set_xlim(0, xmax)

    axes[0].set_title("Token throughput", color=CHART_FG, fontsize=16, fontweight="bold", loc="left", pad=15)
    axes[1].set_title("API-list-equivalent value", color=CHART_FG, fontsize=16, fontweight="bold", loc="left", pad=15)
    axes[0].set_xlabel("Million observed tokens per 1% quota", color=CHART_MUTED, fontsize=10, labelpad=10)
    axes[1].set_xlabel("API-list-equivalent dollars per 1% quota", color=CHART_MUTED, fontsize=10, labelpad=10)

    fig.text(0.055, 0.982, title, color=CHART_FG, fontsize=25, fontweight="bold", ha="left", va="top")
    scope = "Detected policy regimes" if all_regimes or multi_regime_models else "Latest detected policy regime per model"
    fig.text(
        0.055, 0.935,
        f"{scope} · high-water quota buckets · whiskers are {interval:.0%} whole-episode bootstrap intervals",
        color=CHART_MUTED, fontsize=11.5, ha="left",
    )

    handles = [
        Line2D([0], [0], marker="o", linestyle="None", markersize=9,
               markerfacecolor=EFFORT_COLORS.get(e, "#c8d0da"), markeredgecolor="none", label=e)
        for e in present_efforts
    ]
    fig.legend(handles=handles, labels=present_efforts, loc="upper right", bbox_to_anchor=(0.965, 0.982),
               frameon=False, ncol=max(1, len(handles)), labelcolor=CHART_FG, fontsize=10,
               handletextpad=0.4, columnspacing=1.3)

    fig.text(0.055, 0.060,
             "Point labels: value · pt=observed quota points · ep=independent accounting episodes. "
             "Higher means more observed throughput/value per quota point.",
             color=CHART_MUTED, fontsize=9.2, ha="left")
    fig.text(0.055, 0.035,
             "Replayed rollout history is excluded. API prices are a normalization ruler, not plan billing. "
             "Model/effort purity and minimum-evidence thresholds are applied by the analyzer.",
             color=CHART_MUTED, fontsize=9.2, ha="left")

    fig.subplots_adjust(left=0.055, right=0.965, bottom=0.17, top=0.84, wspace=0.22)
    png = prefix + ".png"
    svg = prefix + ".svg"
    Path(png).parent.mkdir(parents=True, exist_ok=True)
    Path(svg).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=180, facecolor=CHART_BG, bbox_inches="tight")
    fig.savefig(svg, facecolor=CHART_BG, bbox_inches="tight")
    plt.close(fig)
    return png, svg


def render_guardian_episode_chart(rows: Sequence[Dict[str, object]], prefix: str) -> Tuple[str, str]:
    """Render Guardian overhead by paired parent model/effort."""
    good = [r for r in rows if r.get("pair_confidence") in {"high", "medium"}
            and int(r.get("parent_tokens", 0) or 0) > 0]
    groups: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    for r in good:
        groups[(str(r.get("parent_model", "unknown")), str(r.get("parent_effort", "unknown")))].append(r)
    groups = {k: v for k, v in groups.items() if len(v) >= 3 and k[0] != "unknown"}
    if not groups:
        raise RuntimeError("no Guardian parent groups with at least 3 medium/high-confidence approval episodes")
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("Guardian charts require matplotlib") from exc

    ordered = sorted(groups)
    fig_h = max(5.5, 0.8 * len(ordered) + 2.6)
    fig, axes = plt.subplots(1, 2, figsize=(15.5, fig_h))
    fig.patch.set_facecolor(CHART_BG)
    for ax in axes:
        ax.set_facecolor(CHART_AX_BG)
        for spine in ax.spines.values():
            spine.set_color(CHART_GRID)
        ax.tick_params(colors=CHART_MUTED)
        ax.xaxis.grid(True, color=CHART_GRID, linewidth=.8)
        ax.set_axisbelow(True)
    ys = list(range(len(ordered)))[::-1]
    labels = [f"{m} {e}" for m, e in ordered]
    for y, key in zip(ys, ordered):
        rs = groups[key]
        vals = [float(r["guardian_tokens"]) / 1e6 for r in rs]
        med, lo, hi = _q(vals,.5), _q(vals,.1), _q(vals,.9)
        axes[0].errorbar(med, y, xerr=[[med-lo],[hi-med]], fmt="o", capsize=4)
        axes[0].text(hi + max(0.01, hi*.02), y, f"{med:.2f}M · {len(rs)}ep", color=CHART_FG, va="center", fontsize=8.5)
        shares = [float(r["guardian_share_local"]) for r in rs if math.isfinite(float(r["guardian_share_local"]))]
        smed, slo, shi = _q(shares,.5), _q(shares,.1), _q(shares,.9)
        axes[1].errorbar(smed*100, y, xerr=[[(smed-slo)*100],[(shi-smed)*100]], fmt="o", capsize=4)
        axes[1].text(shi*100 + 1, y, f"{smed:.0%} · {len(rs)}ep", color=CHART_FG, va="center", fontsize=8.5)
    for ax in axes:
        ax.set_yticks(ys)
        ax.set_yticklabels(labels, color=CHART_FG, fontweight="bold")
    axes[0].set_title("Guardian tokens per approval", color=CHART_FG, fontweight="bold", loc="left")
    axes[1].set_title("Guardian share of local approval context", color=CHART_FG, fontweight="bold", loc="left")
    axes[0].set_xlabel("Million codex-auto-review tokens", color=CHART_MUTED)
    axes[1].set_xlabel("Guardian share of Guardian + paired parent context (%)", color=CHART_MUTED)
    fig.suptitle("Auto-review approval overhead", color=CHART_FG, fontsize=22, fontweight="bold", x=.06, ha="left")
    fig.text(.06,.035,"Medium/high-confidence parent pairs only · whiskers are p10-p90 across approval episodes · quota causality is not implied",
             color=CHART_MUTED, fontsize=9)
    fig.subplots_adjust(left=.18,right=.97,bottom=.13,top=.84,wspace=.25)
    png, svg = prefix + ".png", prefix + ".svg"
    Path(png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png,dpi=180,facecolor=CHART_BG,bbox_inches="tight")
    fig.savefig(svg,facecolor=CHART_BG,bbox_inches="tight")
    plt.close(fig)
    return png, svg


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------

def export_buckets_csv(path: str, buckets: Sequence[Bucket]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "reset_key", "reset_at", "start", "end", "start_used", "end_used", "points", "events",
            "uncached_tokens", "cached_tokens", "output_tokens", "total_tokens",
            "price_coverage", "priced_api_usd", "dominant_model", "dominant_model_share",
            "dominant_effort", "dominant_effort_share",
        ])
        for b in buckets:
            model, share = b.dominant_model
            effort, effort_share = b.dominant_effort
            w.writerow([
                b.reset_key, epoch_to_local(b.reset_at).isoformat(), b.start_ts.isoformat(), b.end_ts.isoformat(),
                b.start_used, b.end_used, b.points, b.events, b.usage.uncached, b.usage.cached,
                b.usage.output, b.total_tokens, b.price_coverage,
                "" if b.usage.api_usd is None else b.usage.api_usd, model, share, effort, effort_share,
            ])


def export_resets_csv(path: str, ledger: Sequence[ResetRecord]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "raw_index", "kind", "creates_accounting_episode", "activation_at",
            "previous_accounting_due_at", "new_due_at", "relative_to_previous_due_hours",
            "first_seen_at", "first_seen_lag_hours", "before_used", "after_used",
            "observed_drop", "raw_reset_key", "accounting_reset_key",
        ])
        for r in ledger:
            w.writerow([
                r.raw_index, r.kind, int(r.creates_episode), r.activation_at.isoformat(),
                "" if r.previous_accounting_due_at is None else r.previous_accounting_due_at.isoformat(),
                r.new_due_at.isoformat(),
                "" if r.relative_to_previous_due is None else r.relative_to_previous_due.total_seconds()/3600,
                r.first_seen_at.isoformat(), r.first_seen_lag.total_seconds()/3600,
                "" if r.before_used is None else r.before_used, r.after_used,
                "" if r.observed_drop is None else r.observed_drop,
                r.raw_reset_key, r.accounting_reset_key,
            ])


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _fake_event(ts: str, used: float, reset: float, model: str = "gpt-5.5",
                unc: int = 100, cached: int = 900, out: int = 50, effort: str = "unknown") -> Event:
    p = price_event(model, unc, cached, out, DEFAULT_PRICES)
    return Event(ts, parse_timestamp(ts), "fake", model, unc, cached, out, used,
                 reset, DEFAULT_WINDOW_MINUTES, p is not None, p, effort=effort)


def run_self_test() -> None:
    # High-water accounting: repeated crossings after backsteps must not create points.
    reset = datetime(2026, 1, 8, tzinfo=timezone.utc).timestamp()
    evs = [
        _fake_event("2026-01-01T00:00:00Z", 0, reset),
        _fake_event("2026-01-01T00:01:00Z", 0, reset),
        _fake_event("2026-01-01T00:02:00Z", 1, reset),
        _fake_event("2026-01-01T00:03:00Z", 1, reset),
        _fake_event("2026-01-01T00:04:00Z", 2, reset),
        _fake_event("2026-01-01T00:05:00Z", 1, reset),
        _fake_event("2026-01-01T00:06:00Z", 1, reset),
        _fake_event("2026-01-01T00:07:00Z", 2, reset),
        _fake_event("2026-01-01T00:08:00Z", 3, reset),
    ]
    bs = high_water_buckets(evs, reset_key(reset, 60), reset)
    assert [b.points for b in bs] == [1, 1, 1], [b.points for b in bs]
    assert bs[-1].events == 4

    ep = summarize_episode(reset_key(reset, 60), evs, DEFAULT_WINDOW_MINUTES)
    assert ep.new_high_points == 3
    assert ep.max_backstep == 1

    # Reset/churn classification. Start with 10% used; an early 10->0 transition is
    # effective, a minute-later 0->0 reset_at change is churn and must be merged,
    # then the next scheduled activation creates a new episode.
    reset2 = datetime(2026, 1, 11, tzinfo=timezone.utc).timestamp()  # activation Jan 4
    reset_churn = datetime(2026, 1, 11, 0, 1, tzinfo=timezone.utc).timestamp()
    reset3 = datetime(2026, 1, 18, tzinfo=timezone.utc).timestamp()  # activation Jan 11
    evs2 = [
        _fake_event("2026-01-04T00:00:00Z", 0, reset2),
        _fake_event("2026-01-04T00:00:30Z", 0, reset2),
    ]
    evs_churn = [_fake_event("2026-01-04T00:01:00Z", 0, reset_churn)]
    evs3 = [_fake_event("2026-01-11T00:01:00Z", 0, reset3)]
    grouped = {
        reset_key(reset, 60): evs,
        reset_key(reset2, 60): evs2,
        reset_key(reset_churn, 60): evs_churn,
        reset_key(reset3, 60): evs3,
    }
    raw_eps = [summarize_episode(k, v, DEFAULT_WINDOW_MINUTES) for k, v in grouped.items()]
    acc_eps, ledger = build_accounting_episodes(
        raw_eps, grouped, DEFAULT_WINDOW_MINUTES, tolerance_hours=2,
        noop_usage_max=1.0, effective_reset_drop=2.0,
    )
    kinds = [r.kind for r in ledger]
    assert kinds == ["initial", "early", "churn", "scheduled"], kinds
    assert len(acc_eps) == 3, len(acc_eps)
    # The churn event must be merged into reset2's accounting episode.
    assert sum(ep.events for ep in acc_eps) == sum(len(v) for v in grouped.values())

    # Price coverage is retained then gated.
    mixed = [
        _fake_event("2026-01-20T00:00:00Z", 0, reset3 + 604800),
        _fake_event("2026-01-20T00:01:00Z", 1, reset3 + 604800, model="unknown-model"),
    ]
    b2 = high_water_buckets(mixed, reset_key(reset3 + 604800, 60), reset3 + 604800)[0]
    assert b2.price_coverage < 1.0
    assert b2.usage.api_usd is not None
    assert not usable_bucket(b2, 0.95)

    # Parser/dedup/replay test: construct a session-history reconstruction prefix
    # followed by one normal live event. The duplicate archived copy is deduped.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        sess = home / "sessions" / "x"
        arch = home / "archived_sessions"
        sess.mkdir(parents=True)
        arch.mkdir(parents=True)
        lines = [json.dumps({"payload": {"type": "turn_context", "model": "gpt-5.5", "effort": "high"}})]
        cin = ccache = cout = 0
        for i in range(30):
            stamp = f"2026-01-03T00:00:00.{i:03d}Z"
            inp, cache, out = 300_000, 250_000, 10_000
            cin += inp; ccache += cache; cout += out
            lines.append(json.dumps({
                "timestamp": stamp,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cache, "output_tokens": out},
                        "total_token_usage": {"input_tokens": cin, "cached_input_tokens": ccache, "output_tokens": cout},
                    },
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"window_minutes": 10080, "used_percent": min(i // 3, 9),
                                    "resets_at": reset + i * 3600},
                    },
                },
            }))
        cin += 10_000; ccache += 8_000; cout += 500
        lines.append(json.dumps({
            "timestamp": "2026-01-03T00:00:10.000Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 10_000, "cached_input_tokens": 8_000, "output_tokens": 500},
                    "total_token_usage": {"input_tokens": cin, "cached_input_tokens": ccache, "output_tokens": cout},
                },
                "rate_limits": {"limit_id": "codex", "primary": {"window_minutes": 10080, "used_percent": 10, "resets_at": reset + 29*3600}},
            },
        }))
        text = "\n".join(lines) + "\n"
        (sess / "a.jsonl").write_text(text, encoding="utf-8")
        (arch / "copy.jsonl").write_text(text, encoding="utf-8")
        loaded, st = load_events(
            str(home), DEFAULT_PRICES, 10080,
            replay_scan_seconds=2.0, replay_min_events=20,
            replay_min_growth_mtokens=5.0, replay_start_max_mtokens=2.0,
            dense_threshold=20,
        )
        assert st.global_duplicates == 31, st.global_duplicates
        assert st.replay_prefixes == 1, st.replay_prefixes
        assert st.replay_events == 30, st.replay_events
        assert len(loaded) == 31
        assert all(e.probable_replay for e in loaded[:30])
        assert not loaded[-1].probable_replay
        assert all(e.effort == "high" for e in loaded), {e.effort for e in loaded}
        assert st.effort_direct_observations >= 2

        # Fallback effort extraction and conflict audit are deterministic.
        est = ParseStats()
        assert effort_from_payload({"thread_settings": {"reasoning_effort": "xhigh"}}, est) == "xhigh"
        assert est.effort_fallback_observations == 1
        est2 = ParseStats()
        assert effort_from_payload({"effort": "high", "collaboration_mode": {"settings": {"reasoning_effort": "xhigh"}}}, est2) == "high"
        assert est2.effort_conflicts == 1

        # Guardian/reviewer metadata and multi-window extraction use only coarse,
        # privacy-safe state. Primary/secondary order does not matter.
        assert source_kind_from_payload({"source": {"subagent": {"name": "guardian"}}}) == "subagent"
        pol, rev = approval_state_from_payload({
            "thread_settings": {"approval_policy": "never", "approvals_reviewer": "auto_review"}
        })
        assert (pol, rev) == ("never", "auto_review")
        wins = extract_rate_windows({
            "limit_id": "codex",
            "primary": {"window_minutes": 300, "used_percent": 12, "resets_at": reset},
            "secondary": {"window_minutes": 10080, "used_percent": 34, "resets_at": reset3},
        })
        assert wins[300].used == 12 and wins[10080].used == 34
        ge = _fake_event("2026-01-03T01:00:00Z", 1, reset, model="codex-auto-review", effort="low")
        ge.source_kind = "subagent"
        ge.approvals_reviewer = "auto_review"
        ge.rate_windows = wins
        assert ge.is_auto_review_inference and ge.is_confirmed_guardian
        assert events_for_window([ge], 300)[0].used == 12

        # Privacy-safe linkage + temporal Guardian pairing.
        li_g = SessionLinkInfo(); li_p = SessionLinkInfo()
        _collect_link_ids({"source": {"subagent": {"thread_id": "parent-thread-123456"}}}, li_g)
        _collect_link_ids({"thread_id": "parent-thread-123456"}, li_p)
        pst = ParseStats(session_links={"guardian": li_g, "parent": li_p})
        p1 = _fake_event("2026-01-03T00:59:55Z", 0, reset, model="gpt-5.5", effort="high")
        p1.source = "parent"; p1.approvals_reviewer = "auto_review"
        g1 = _fake_event("2026-01-03T01:00:00Z", 1, reset, model="codex-auto-review", effort="low")
        g1.source = "guardian"; g1.source_kind = "subagent"; g1.approvals_reviewer = "auto_review"
        pargs = argparse.Namespace(guardian_parent_match_seconds=300.0)
        pair = _pair_guardian_group([g1], "guardian", {"guardian":[g1],"parent":[p1]}, pst, pargs)
        assert pair.parent_source == "parent" and pair.confidence == "high", pair

    # Token-weight fitter: recover a simple two-weight input/output relationship
    # from multiple independent episodes, while a deliberately collinear full design
    # must be flagged by the condition diagnostic.
    synth_rows = []
    for eid in range(1, 7):
        for j in range(1, 5):
            # points = 0.20 * input_M + 2.0 * output_M
            inp_m = 2.0 + eid * 0.5 + j * 0.3
            out_m = 0.03 + eid * 0.01 + j * 0.02
            pts = 0.20 * inp_m + 2.0 * out_m
            usage = Usage(int(inp_m * 0.15 * 1e6), int(inp_m * 0.85 * 1e6), int(out_m * 1e6), 1.0)
            fake_b = Bucket(eid, reset, evs[0].ts, evs[-1].ts, 0, pts, pts, 1, usage,
                            usage.tokens, usage.tokens, {"gpt-5.5": usage.tokens})
            synth_rows.append(WeightRow(fake_b, eid, "gpt-5.5", 1.0))
    io_beta = _nnls(synth_rows, "io")
    assert abs(io_beta[0] - 0.20) < 0.02, io_beta
    assert abs(io_beta[1] - 2.0) < 0.2, io_beta
    cv, _ = _episode_cv(synth_rows, "io")
    assert cv < 0.05, cv

    # Automatic regime detection should split a persistent ~2x efficiency change.
    rr = []
    base_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, metric in enumerate([10, 10.5, 9.8, 21, 20.5, 22]):
        rr.append({"episode_id": i, "start": base_dt + timedelta(days=i),
                   "end": base_dt + timedelta(days=i, hours=1), "points": 25.0,
                   "api": metric, "tokens_per_point": metric, "metric": metric})
    spans = _regime_splits(rr, min_episodes=3, min_points=50, min_ratio=1.3, min_improvement=.45)
    assert spans == [(0, 3), (3, 6)], spans

    # Perfect proportionality among the full-model predictors is non-identifiable.
    col_rows = []
    for eid in range(1, 7):
        for j in range(1, 4):
            u = float(eid + j); c = 10*u; o = 0.1*u
            usage = Usage(int(u*1e6), int(c*1e6), int(o*1e6), 1.0)
            pts = 0.1*u + 0.01*c + 1.0*o
            fake_b = Bucket(eid, reset, evs[0].ts, evs[-1].ts, 0, pts, pts, 1, usage,
                            usage.tokens, usage.tokens, {"gpt-5.5": usage.tokens})
            col_rows.append(WeightRow(fake_b, eid, "gpt-5.5", 1.0))
    assert _design_condition(col_rows, "full") > 1e6

    # Chart aggregation keeps effort separate and bootstraps whole episodes.
    chart_buckets = []
    for eid in range(1, 7):
        for effort_name, toks_m, pts in (("high", 12.0, 2.0), ("xhigh", 10.0, 2.0)):
            usage = Usage(int(toks_m * 0.05 * 1e6), int(toks_m * 0.94 * 1e6), int(toks_m * 0.01 * 1e6), pts * 10.0)
            tb = usage.tokens
            b = Bucket(
                eid, reset, evs[0].ts + timedelta(days=eid), evs[0].ts + timedelta(days=eid, minutes=1),
                0, pts, pts, 1, usage, tb, tb,
                {"gpt-5.5": tb}, {effort_name: tb},
            )
            chart_buckets.append(b)
    ca = argparse.Namespace(
        chart_model_purity=.95, chart_effort_purity=.95, min_price_coverage=.95,
        regime_min_episodes=3, regime_min_points=5.0, regime_min_ratio=1.3, regime_min_improvement=.45,
        chart_all_regimes=False, chart_min_points=10.0, chart_min_episodes=3,
        chart_bootstraps=50, chart_interval=.80,
    )
    cr = build_chart_rows(chart_buckets, ca)
    assert {r["effort"] for r in cr} == {"high", "xhigh"}, cr
    assert all(r["episodes"] == 6 for r in cr), cr
    assert all(float(r["tokens_p10_m"]) == float(r["tokens_p10_m"]) for r in cr)

    print("self-test: OK")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def build_parser() -> argparse.ArgumentParser:
    epilog = """Examples:
  python3 codex_quota_audit.py
      Run the full text analysis. No third-party packages required.

  python3 codex_quota_audit.py --charts
      Run the analysis and create quota_chart_data.csv plus PNG/SVG charts.
      Charts require matplotlib.

  python3 codex_quota_audit.py --charts --chart-all-regimes
      Plot every detected historical policy regime instead of only the latest.

  python3 codex_quota_audit.py --weight-details
      Show detailed token-weight fit diagnostics.

  python3 codex_quota_audit.py --export-guardian-buckets guardian_buckets.csv
      Export conservative 5h/7d auto-review attribution buckets.

  python3 codex_quota_audit.py --export-approval-episodes approval_episodes.csv
      Export parent-paired Guardian approval episodes and quota envelopes.

Chart setup (recommended):
  python3 -m venv .venv
  source .venv/bin/activate
  python3 -m pip install matplotlib

Everything except --charts uses only the Python standard library.
"""
    p = argparse.ArgumentParser(
        description=(
            "Audit local Codex usage with effective-reset reconstruction, high-water quota "
            "accounting, replay detection, model/effort comparisons, Guardian parent-pairing and "
            "approval-episode overhead diagnostics, and experimental token-type quota-weight estimation."
        ),
        epilog=epilog,
        formatter_class=_HelpFormatter,
    )

    common = p.add_argument_group("Common options")
    common.add_argument("--home", default="~/.codex", help="Codex data directory")
    common.add_argument("--prices", help="JSON file overriding/extending API normalization prices")
    common.add_argument("--include-replays", action="store_true",
                        help="include probable replay prefixes in the primary analysis (debug/sensitivity)")
    common.add_argument("--include-suspect-bursts", action="store_true", dest="include_replays", help=argparse.SUPPRESS)
    common.add_argument("--show-reset-churn", action="store_true",
                        help="show near-zero resets_at churn rows in the reset ledger")
    common.add_argument("--weight-details", action="store_true",
                        help="show every candidate token-weight model and its diagnostics")
    common.add_argument("--no-weight-analysis", action="store_true",
                        help="skip the experimental token-type quota-weight analysis")
    common.add_argument("--no-guardian-audit", action="store_true",
                        help="skip Guardian parent-pairing, approval episodes, and multi-window quota analysis")
    common.add_argument("--self-test", action="store_true", help="run built-in synthetic tests and exit")
    common.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="show version and exit")

    output = p.add_argument_group("Output and charts")
    output.add_argument("--export-buckets", metavar="PATH", help="write high-water quota buckets to CSV")
    output.add_argument("--export-resets", metavar="PATH", help="write inferred reset ledger to CSV")
    output.add_argument("--export-chart-data", metavar="PATH", help="write model x effort chart aggregates to CSV")
    output.add_argument("--export-guardian-buckets", metavar="PATH",
                        help="write Guardian/auto-review high-water attribution buckets to CSV")
    output.add_argument("--export-approval-episodes", metavar="PATH",
                        help="write parent-paired Guardian approval episodes and quota envelopes to CSV")
    output.add_argument("--charts", action="store_true",
                        help="write quota_chart_data.csv plus model/effort PNG and SVG (requires matplotlib)")
    output.add_argument("--chart-prefix", default="quota_value_by_model_effort",
                        help="output path prefix for --charts PNG/SVG")
    output.add_argument("--guardian-chart-prefix", default="guardian_approval_overhead",
                        help="output path prefix for Guardian approval-overhead PNG/SVG")
    output.add_argument("--chart-all-regimes", action="store_true",
                        help="include every detected policy regime instead of only the latest per model")

    advanced = p.add_argument_group("Advanced analysis options")
    advanced.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES,
                          help="rate-limit window to analyze")
    advanced.add_argument("--min-price-coverage", type=float, default=DEFAULT_MIN_PRICE_COVERAGE,
                          help="minimum fraction of raw tokens priced before API$/pt is shown")
    advanced.add_argument("--model-purity", type=float, default=DEFAULT_MODEL_PURITY,
                          help="minimum raw-token share for a bucket to be assigned to one model")
    advanced.add_argument("--model-time-min-points", type=float, default=5.0,
                          help="minimum quota points for a model x month row")

    guardian = p.add_argument_group("Guardian / auto-review audit")
    guardian.add_argument("--guardian-purity", type=float, default=DEFAULT_GUARDIAN_PURITY,
                          help="minimum codex-auto-review raw-token share for a Guardian-dominant quota bucket")
    guardian.add_argument("--guardian-isolation-seconds", type=float, default=DEFAULT_GUARDIAN_ISOLATION_SECONDS,
                          help="legacy local-isolation margin for Guardian high-water bucket diagnostics")
    guardian.add_argument("--guardian-parent-match-seconds", type=float, default=DEFAULT_GUARDIAN_PARENT_MATCH_SECONDS,
                          help="maximum temporal distance for heuristic Guardian-to-parent pairing")
    guardian.add_argument("--guardian-episode-gap-seconds", type=float, default=DEFAULT_GUARDIAN_EPISODE_GAP_SECONDS,
                          help="gap that starts a new Guardian approval episode")
    guardian.add_argument("--guardian-context-seconds", type=float, default=DEFAULT_GUARDIAN_CONTEXT_SECONDS,
                          help="parent-work context captured before/after each Guardian episode")
    guardian.add_argument("--guardian-quota-snapshot-seconds", type=float, default=DEFAULT_GUARDIAN_QUOTA_SNAPSHOT_SECONDS,
                          help="maximum age/gap for quota snapshots around an approval episode")
    guardian.add_argument("--guardian-match-token-ratio", type=float, default=DEFAULT_GUARDIAN_MATCH_TOKEN_RATIO,
                          help="maximum parent-token ratio for matched auto-vs-manual approval controls")
    guardian.add_argument("--guardian-fit-bootstraps", type=int, default=DEFAULT_GUARDIAN_FIT_BOOTSTRAPS,
                          help="parent-session bootstrap replicates for exploratory incremental Guardian fit")

    replay = p.add_argument_group("Advanced replay detection")
    replay.add_argument("--replay-scan-seconds", type=float, default=DEFAULT_REPLAY_SCAN_SECONDS,
                        help="opening seconds of each rollout file examined for cumulative-history reconstruction")
    replay.add_argument("--replay-min-events", type=int, default=DEFAULT_REPLAY_MIN_EVENTS,
                        help="minimum opening events before replay classification is considered")
    replay.add_argument("--replay-min-growth-mtokens", type=float, default=DEFAULT_REPLAY_MIN_GROWTH_MTOKENS,
                        help="minimum cumulative-token growth in the opening scan for probable replay")
    replay.add_argument("--replay-start-max-mtokens", type=float, default=DEFAULT_REPLAY_START_MAX_MTOKENS,
                        help="maximum starting cumulative tokens for a replay reconstruction prefix")
    replay.add_argument("--dense-threshold", type=int, default=DEFAULT_DENSE_THRESHOLD,
                        help="events per file-second above which an unproven dense group is reported diagnostically")

    reset = p.add_argument_group("Advanced reset accounting")
    reset.add_argument("--reset-tolerance", type=int, default=DEFAULT_RESET_TOLERANCE_SECONDS,
                       help="seconds of tolerance when clustering near-identical resets_at values")
    reset.add_argument("--reset-class-tolerance-hours", type=float, default=DEFAULT_RESET_CLASS_TOLERANCE_HOURS,
                       help="timing tolerance for calling a reset scheduled vs early/after-due")
    reset.add_argument("--noop-usage-max", type=float, default=DEFAULT_NOOP_USAGE_MAX,
                       help="max used_percent on both sides for treating an early resets_at change as no-op churn")
    reset.add_argument("--effective-reset-drop", type=float, default=DEFAULT_EFFECTIVE_RESET_DROP,
                       help="minimum observed used_percent drop for calling a non-scheduled transition an effective reset")

    weights = p.add_argument_group("Advanced token-weight and regime fitting")
    weights.add_argument("--weight-model-purity", type=float, default=DEFAULT_WEIGHT_MODEL_PURITY,
                         help="minimum dominant-model token share for token-weight fitting")
    weights.add_argument("--weight-min-points", type=float, default=DEFAULT_WEIGHT_MIN_POINTS,
                         help="minimum quota points in a model-policy regime before fitting weights")
    weights.add_argument("--weight-min-episodes", type=int, default=DEFAULT_WEIGHT_MIN_EPISODES,
                         help="minimum independent reset episodes in a regime before fitting weights")
    weights.add_argument("--weight-bootstraps", type=int, default=DEFAULT_WEIGHT_BOOTSTRAPS,
                         help="whole-episode bootstrap replicates for token-weight intervals")
    weights.add_argument("--weight-max-condition", type=float, default=DEFAULT_WEIGHT_MAX_CONDITION,
                         help="maximum standardized design condition number before a fit is called collinear")
    weights.add_argument("--weight-max-cv-wape", type=float, default=DEFAULT_WEIGHT_MAX_CV_WAPE,
                         help="maximum held-out episode weighted absolute percentage error for a supported fit")
    weights.add_argument("--weight-cv-tolerance", type=float, default=DEFAULT_WEIGHT_CV_TOLERANCE,
                         help="prefer a simpler supported model when its CV error is within this fraction of the best")
    weights.add_argument("--regime-min-episodes", type=int, default=DEFAULT_REGIME_MIN_EPISODES,
                         help="minimum episodes on each side of an automatic policy-regime break")
    weights.add_argument("--regime-min-points", type=float, default=DEFAULT_REGIME_MIN_POINTS,
                         help="minimum quota points on each side of an automatic policy-regime break")
    weights.add_argument("--regime-min-ratio", type=float, default=DEFAULT_REGIME_MIN_RATIO,
                         help="minimum efficiency ratio across an automatic policy-regime break")
    weights.add_argument("--regime-min-improvement", type=float, default=DEFAULT_REGIME_MIN_IMPROVEMENT,
                         help="minimum weighted log-SSE improvement required for a policy-regime break")

    chart = p.add_argument_group("Advanced chart options")
    chart.add_argument("--chart-model-purity", type=float, default=DEFAULT_CHART_MODEL_PURITY,
                       help="minimum dominant-model token share for chart buckets")
    chart.add_argument("--chart-effort-purity", type=float, default=DEFAULT_CHART_EFFORT_PURITY,
                       help="minimum dominant-effort token share for chart buckets")
    chart.add_argument("--chart-min-points", type=float, default=DEFAULT_CHART_MIN_POINTS,
                       help="minimum quota points for a model x effort chart row")
    chart.add_argument("--chart-min-episodes", type=int, default=DEFAULT_CHART_MIN_EPISODES,
                       help="minimum independent accounting episodes for a chart row")
    chart.add_argument("--chart-bootstraps", type=int, default=DEFAULT_CHART_BOOTSTRAPS,
                       help="whole-episode bootstrap replicates for chart whiskers")
    chart.add_argument("--chart-interval", type=float, default=DEFAULT_CHART_INTERVAL,
                       help="central bootstrap interval width for chart whiskers")
    return p


def validate_args(args: argparse.Namespace) -> None:
    if not (0.0 <= args.min_price_coverage <= 1.0):
        raise SystemExit("--min-price-coverage must be between 0 and 1")
    if not (0.0 <= args.model_purity <= 1.0):
        raise SystemExit("--model-purity must be between 0 and 1")
    if args.window_minutes <= 0:
        raise SystemExit("--window-minutes must be positive")
    if not (0.0 <= args.guardian_purity <= 1.0):
        raise SystemExit("--guardian-purity must be between 0 and 1")
    if args.guardian_isolation_seconds < 0:
        raise SystemExit("--guardian-isolation-seconds must be non-negative")
    if args.guardian_parent_match_seconds < 0 or args.guardian_episode_gap_seconds < 0:
        raise SystemExit("Guardian pairing/gap seconds must be non-negative")
    if args.guardian_context_seconds < 0 or args.guardian_quota_snapshot_seconds < 0:
        raise SystemExit("Guardian context/snapshot seconds must be non-negative")
    if args.guardian_match_token_ratio < 1.0:
        raise SystemExit("--guardian-match-token-ratio must be >= 1")
    if args.guardian_fit_bootstraps < 0:
        raise SystemExit("--guardian-fit-bootstraps must be non-negative")
    if args.reset_tolerance <= 0:
        raise SystemExit("--reset-tolerance must be positive")
    if args.reset_class_tolerance_hours < 0:
        raise SystemExit("--reset-class-tolerance-hours must be non-negative")
    if args.noop_usage_max < 0:
        raise SystemExit("--noop-usage-max must be non-negative")
    if args.effective_reset_drop < 0:
        raise SystemExit("--effective-reset-drop must be non-negative")
    if args.model_time_min_points < 0:
        raise SystemExit("--model-time-min-points must be non-negative")
    if not (0.0 <= args.weight_model_purity <= 1.0):
        raise SystemExit("--weight-model-purity must be between 0 and 1")
    if args.weight_min_points < 0 or args.weight_min_episodes < 1:
        raise SystemExit("weight minimums must be non-negative points and at least one episode")
    if args.weight_bootstraps < 0:
        raise SystemExit("--weight-bootstraps must be non-negative")
    if args.weight_max_condition <= 0 or args.weight_max_cv_wape < 0 or args.weight_cv_tolerance < 0:
        raise SystemExit("weight diagnostic thresholds must be positive/non-negative")
    if args.regime_min_episodes < 1 or args.regime_min_points < 0:
        raise SystemExit("regime minimums must be at least one episode and non-negative points")
    if args.regime_min_ratio < 1.0 or not (0.0 <= args.regime_min_improvement <= 1.0):
        raise SystemExit("--regime-min-ratio must be >=1 and --regime-min-improvement between 0 and 1")
    if not (0.0 <= args.chart_model_purity <= 1.0) or not (0.0 <= args.chart_effort_purity <= 1.0):
        raise SystemExit("chart purity thresholds must be between 0 and 1")
    if args.chart_min_points < 0 or args.chart_min_episodes < 1:
        raise SystemExit("chart minimums must be non-negative points and at least one episode")
    if args.chart_bootstraps < 0 or not (0.0 < args.chart_interval < 1.0):
        raise SystemExit("--chart-bootstraps must be non-negative and --chart-interval between 0 and 1")
    if args.replay_scan_seconds <= 0:
        raise SystemExit("--replay-scan-seconds must be positive")
    if args.replay_min_events < 2:
        raise SystemExit("--replay-min-events must be at least 2")
    if args.replay_min_growth_mtokens < 0 or args.replay_start_max_mtokens < 0:
        raise SystemExit("replay token thresholds must be non-negative")
    if args.dense_threshold < 0:
        raise SystemExit("--dense-threshold must be non-negative")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args)

    if args.self_test:
        run_self_test()
        return 0

    try:
        prices = load_prices(args.prices)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(f"could not load prices: {exc}")
        return 2

    all_records, stats = load_events(
        os.path.expanduser(args.home), prices, args.window_minutes,
        args.replay_scan_seconds, args.replay_min_events,
        args.replay_min_growth_mtokens, args.replay_start_max_mtokens,
        args.dense_threshold,
    )
    if not all_records:
        print("No analyzable Codex events found.")
        print(f"Searched: {os.path.expanduser(args.home)}")
        return 1

    filtered_records = [e for e in all_records if not e.probable_replay]
    primary_records = all_records if args.include_replays else filtered_records
    if not primary_records:
        print("No events remain in the primary analysis after replay filtering.")
        print("Try --include-replays or relax the replay thresholds.")
        return 1

    all_target_events = events_for_window(all_records, args.window_minutes)
    filtered_target_events = events_for_window(filtered_records, args.window_minutes)
    primary_events = events_for_window(primary_records, args.window_minutes)
    if not primary_events:
        print(f"No events contain the requested {args.window_minutes}-minute Codex limit.")
        print("Discovered windows: " + ", ".join(str(w) for w in discovered_windows(primary_records)))
        return 1
    stats.analyzed_events = len(primary_events)

    primary = analyze_events(primary_events, args)
    print_header(args, stats, primary)
    print_reset_ledger(primary.ledger, args.show_reset_churn)
    print_episodes(primary.episodes, args.min_price_coverage)
    print_monthly(primary.events, primary.buckets, args.min_price_coverage)
    print_model_time(primary.buckets, args.min_price_coverage, args.model_purity,
                     args.model_time_min_points)
    if not args.no_weight_analysis:
        print_weight_analysis(primary.buckets, args)

    # Sensitivity comparison uses the exact same parsed/deduped dataset.
    if stats.replay_events and filtered_target_events:
        filtered_analysis = primary if not args.include_replays else analyze_events(filtered_target_events, args)
        included_analysis = primary if args.include_replays else analyze_events(all_target_events, args)
        print_replay_sensitivity(filtered_analysis, included_analysis, args.min_price_coverage)

    guardian_rows: List[Dict[str, object]] = []
    approval_episodes: List[Dict[str, object]] = []
    if not args.no_guardian_audit or args.export_guardian_buckets or args.export_approval_episodes or args.charts:
        guardian_rows = guardian_bucket_rows(primary_records, args)
        approval_episodes = build_approval_episodes(primary_records, stats, args, primary)
    if not args.no_guardian_audit:
        print_guardian_audit(primary_records, args, guardian_rows, stats, approval_episodes, primary)

    print_unpriced(primary.events, prices)
    print_notes(primary)

    if args.export_buckets:
        export_buckets_csv(args.export_buckets, primary.buckets)
        print(f"\nWrote bucket CSV: {args.export_buckets}")
    if args.export_resets:
        export_resets_csv(args.export_resets, primary.ledger)
        print(f"Wrote reset ledger CSV: {args.export_resets}")
    if args.export_guardian_buckets:
        export_guardian_buckets_csv(args.export_guardian_buckets, guardian_rows)
        print(f"Wrote Guardian bucket CSV: {args.export_guardian_buckets}")
    if args.export_approval_episodes:
        export_approval_episodes_csv(args.export_approval_episodes, approval_episodes)
        print(f"Wrote approval episode CSV: {args.export_approval_episodes}")

    if args.export_chart_data or args.charts:
        chart_rows = build_chart_rows(primary.buckets, args)
        print_chart_data_summary(chart_rows, args)
        chart_csv = args.export_chart_data or "quota_chart_data.csv"
        export_chart_data_csv(chart_csv, chart_rows)
        print(f"Wrote chart CSV: {chart_csv}")
        if args.charts:
            try:
                png, svg = render_quota_chart(
                    chart_rows, args.chart_prefix, args.chart_interval, args.chart_all_regimes
                )
                print(f"Wrote charts: {png}, {svg}")
            except RuntimeError as exc:
                print(f"Chart rendering unavailable: {exc}", file=sys.stderr)
                print("The chart CSV was still written.", file=sys.stderr)
        if approval_episodes:
            try:
                gpng, gsvg = render_guardian_episode_chart(approval_episodes, args.guardian_chart_prefix)
                print(f"Wrote Guardian approval charts: {gpng}, {gsvg}")
            except RuntimeError as exc:
                print(f"Guardian approval chart unavailable: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
