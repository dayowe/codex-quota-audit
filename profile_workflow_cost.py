#!/usr/bin/env python3
"""
Codex workflow cost profiler v6.1

Companion to find_workflow_candidates.py and extract_workflow_lifecycle.py.

This profiler deliberately does NOT require exact SEND/WAIT session-recipient recovery.
Instead it uses trusted spawn events to build observed descendant-lifetime
windows, including unknown roles, then measures where inference accumulates.
Generic mode assumes no workflow sequence. The staged profile or explicit role
pairs enable optional sequence interpretation; neither changes core accounting.
When SEND arguments contain an exact configured role label, that role-level target
is treated as trusted even though the specific child session remains unknown.
v4 adds restart-aware time windows and conservative chunk/assignment correlation.
v4.1 fixes cutoff reconstruction for resumed/forked sessions whose recorded earliest
timestamp can predate their actual activation: orchestrator selection now uses
post-window observed activity, and trusted spawn time is the effective child start.
v4.2 adds an explicit context-compaction audit: it counts persisted compaction
events, conservatively associates directly observed compaction token usage when
available, measures context shrink/refill, and profiles post-compaction recovery
work including privacy-safe repeated resource-access signals.
It is intended to answer optimization questions such as:

- How much work is spent in the orchestrator vs implementers vs validators?
- How much orchestrator inference occurs while implementers, validators, or both are active?
- Which stages repeatedly reread very large cached contexts for small outputs?
- Do long-lived agents repeatedly re-enter inference after idle gaps?
- What does each observed implement -> validate cycle cost, and what is its supervision ratio?
- Is observed overlap same-chunk repair/replacement work, cross-chunk work, or unclassified?
- Did workflow behavior change after a known restart/update boundary?
- How often does each agent compact, what direct token usage is observable, and
  how much work occurs while the context is rebuilding afterward?
- Does post-compaction recovery repeatedly revisit the same files/resources or
  perform more inference than the same session's non-recovery baseline?
- Is there a structural tool-call field that could bridge spawn results to
  SEND/WAIT targets in a future extractor version?

Privacy properties:
- profiling is read-only; no network requests
- never prints prompts, model responses, source code, tool stdout, or raw IDs
- uses the same one-way-hashed W-/S-/Axx identifiers as the workflow helpers
- action-schema audit prints only field paths, types, counts, and match counts
- JSON export contains structural metadata and aggregate token counts only
- optional pause review saves local annotations; it never edits source logs

API-dollar values are public API-list-price equivalents used only as a
normalization ruler. They are not subscription billing or OpenAI internal cost.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import re
import shlex
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import find_workflow_candidates as finder
    import extract_workflow_lifecycle as lifecycle
    import codex_quota_audit as audit
    import workflow_attribution as attribution
    import workflow_pauses as pauses
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "profile_workflow_cost.py must be in the same directory as "
        "find_workflow_candidates.py, extract_workflow_lifecycle.py, "
        "codex_quota_audit.py, workflow_attribution.py, and workflow_pauses.py"
    ) from exc

__version__ = "6.1"

DEFAULT_SUCCESSOR_MAP = {
    "planner": "implementer",
    "implementer": "validator",
    "validator": "implementer",
}


@dataclass
class TokenTotals:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    api_eq: float = 0.0
    priced_tokens: int = 0
    total_tokens_for_coverage: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cached_input_tokens, 0)

    @property
    def cache_share(self) -> float:
        return self.cached_input_tokens / self.input_tokens if self.input_tokens else float("nan")

    @property
    def price_coverage(self) -> float:
        if not self.total_tokens_for_coverage:
            return float("nan")
        return self.priced_tokens / self.total_tokens_for_coverage

    def add_request(self, req: lifecycle.UsageRequest,
                    prices: Dict[str, Tuple[float, float, float]]) -> None:
        self.requests += 1
        self.input_tokens += req.input_tokens
        self.cached_input_tokens += req.cached_input_tokens
        self.output_tokens += req.output_tokens
        self.reasoning_tokens += req.reasoning_tokens
        raw = req.total_tokens
        self.total_tokens_for_coverage += raw
        eq = request_api_eq(req, prices)
        if eq is not None:
            self.api_eq += eq
            self.priced_tokens += raw

    def add_totals(self, other: "TokenTotals") -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.api_eq += other.api_eq
        self.priced_tokens += other.priced_tokens
        self.total_tokens_for_coverage += other.total_tokens_for_coverage


@dataclass
class ActiveWindow:
    index: int
    role: str
    target_key: str
    target_label: str
    spawn_ts: datetime
    start: datetime
    end: datetime
    spawn_method: str
    spawn_confidence: str
    chunk_key: Optional[str] = None
    chunk_label: Optional[str] = None
    assignment_key: Optional[str] = None
    assignment_source: Optional[str] = None
    assignment_confidence: Optional[str] = None

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass
class LingeringExposure:
    window: ActiveWindow
    trigger_window: ActiveWindow
    trigger_ts: datetime
    reason: str
    classification: str
    child_after_trigger: TokenTotals
    root_during_lingering: TokenTotals

    @property
    def lingering_seconds(self) -> float:
        return max(0.0, (self.window.end - self.trigger_ts).total_seconds())

    @property
    def active_after_trigger(self) -> bool:
        return self.child_after_trigger.requests > 0


@dataclass
class Burst:
    session_key: str
    agent_label: str
    role: str
    index: int
    start: datetime
    end: datetime
    totals: TokenTotals = field(default_factory=TokenTotals)
    first_input: int = 0
    last_input: int = 0
    peak_input: int = 0

    @property
    def duration_seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass
class Cycle:
    index: int
    implementer_window: ActiveWindow
    validator_window: ActiveWindow
    handoff_gap_seconds: float
    quality: str
    root_implementer: TokenTotals
    root_validator: TokenTotals
    implementer_agent: TokenTotals
    validator_agent: TokenTotals
    guardian: TokenTotals
    all_work: TokenTotals
    other_active_roles: Tuple[str, ...] = ()
    other_active_agents: Tuple[str, ...] = ()

    @property
    def isolated(self) -> bool:
        return not self.other_active_agents

    @property
    def ratio_eligible(self) -> bool:
        return self.isolated and self.quality in {"tight-sequential", "sequential"}

    @property
    def root_combined_api_eq(self) -> float:
        return self.root_implementer.api_eq + self.root_validator.api_eq

    @property
    def child_combined_api_eq(self) -> float:
        return self.implementer_agent.api_eq + self.validator_agent.api_eq

    @property
    def supervision_ratio_api_eq(self) -> float:
        return self.root_combined_api_eq / self.child_combined_api_eq if self.child_combined_api_eq else float("nan")

    @property
    def implementation_supervision_ratio_api_eq(self) -> float:
        return self.root_implementer.api_eq / self.implementer_agent.api_eq if self.implementer_agent.api_eq else float("nan")

    @property
    def validation_supervision_ratio_api_eq(self) -> float:
        return self.root_validator.api_eq / self.validator_agent.api_eq if self.validator_agent.api_eq else float("nan")


@dataclass
class SchemaStat:
    occurrences: int = 0
    calls_present: set[str] = field(default_factory=set)
    types: Counter = field(default_factory=Counter)
    scalar_hashes: set[str] = field(default_factory=set)
    id_shaped: int = 0
    family_id_matches: int = 0
    role_label_hits: int = 0
    spawn_handle_overlap: int = 0


@dataclass
class SchemaAudit:
    call_totals: Counter = field(default_factory=Counter)
    stats: Dict[Tuple[str, str, str], SchemaStat] = field(default_factory=dict)
    spawn_output_token_to_children: Dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    action_arg_tokens: Dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    bridge_candidates: Counter = field(default_factory=Counter)


@dataclass
class AssignmentFingerprint:
    call_id: str
    chunk_key: str
    assignment_key: str
    role: str
    source: str
    confidence: str


@dataclass
class AnalysisMeta:
    requested_after: Optional[datetime] = None
    requested_before: Optional[datetime] = None
    analysis_start: Optional[datetime] = None
    analysis_end: Optional[datetime] = None
    source_family_start: Optional[datetime] = None
    source_family_end: Optional[datetime] = None
    original_family_root: Optional[str] = None
    analysis_root: Optional[str] = None
    root_selection_method: str = "family-root"
    root_selection_confidence: str = "high"
    membership_method: str = "full-family"
    pre_window: List[str] = field(default_factory=list)
    carry_in: List[str] = field(default_factory=list)
    in_window: List[str] = field(default_factory=list)
    post_window: List[str] = field(default_factory=list)
    carry_out: List[str] = field(default_factory=list)
    excluded_in_window: List[str] = field(default_factory=list)
    orchestrator_candidates: List[dict] = field(default_factory=list)
    carry_in_totals: TokenTotals = field(default_factory=TokenTotals)

    @property
    def windowed(self) -> bool:
        return self.requested_after is not None or self.requested_before is not None


@dataclass
class RawUsageSample:
    ts: datetime
    request: lifecycle.UsageRequest
    cumulative_total: Optional[int]


@dataclass
class CompactionMarker:
    ts: datetime
    kind: str


@dataclass
class ToolAccessEvent:
    ts: datetime
    category: str
    resource_hashes: frozenset[str] = frozenset()


@dataclass
class RawCompactionSession:
    markers: List[CompactionMarker] = field(default_factory=list)
    usage: List[RawUsageSample] = field(default_factory=list)
    tools: List[ToolAccessEvent] = field(default_factory=list)
    parse_errors: int = 0
    tool_duplicates: int = 0


@dataclass
class CompactionEvent:
    index: int
    session_key: str
    agent_label: str
    role: str
    ts: datetime
    completion_ts: datetime
    marker_kinds: Tuple[str, ...]
    direct_usage: Optional[lifecycle.UsageRequest] = None
    direct_api_eq: Optional[float] = None
    direct_usage_in_primary_totals: bool = False
    direct_usage_method: str = "unobserved"
    direct_usage_confidence: str = "none"
    before_input_tokens: Optional[int] = None
    after_input_tokens: Optional[int] = None
    shrink_fraction: Optional[float] = None
    recovery_end: Optional[datetime] = None
    recovery_end_reason: str = "session-end"
    recovery_totals: TokenTotals = field(default_factory=TokenTotals)
    recovery_duration_seconds: float = 0.0
    recovery_peak_input_tokens: Optional[int] = None
    refill_target_tokens: Optional[int] = None
    refill_reached: bool = False
    tool_counts: Counter = field(default_factory=Counter)
    tool_events: int = 0
    unique_resources_after: int = 0
    pre_resources: int = 0
    repeated_resources: int = 0
    repeated_resource_access_events: int = 0
    repeated_read_resources: int = 0
    repeated_read_events: int = 0


@dataclass
class CompactionAudit:
    events: List[CompactionEvent] = field(default_factory=list)
    direct_totals: TokenTotals = field(default_factory=TokenTotals)
    direct_outside_primary_totals: TokenTotals = field(default_factory=TokenTotals)
    direct_matched: int = 0
    direct_in_primary_count: int = 0
    recovery_totals: TokenTotals = field(default_factory=TokenTotals)
    recovery_request_keys: set[Tuple[str, datetime, int, int, int]] = field(default_factory=set)
    baseline_totals: TokenTotals = field(default_factory=TokenTotals)
    baseline_tool_events: int = 0
    recovery_tool_events: int = 0
    baseline: dict = field(default_factory=dict)
    by_role: Dict[str, dict] = field(default_factory=dict)
    tool_activity: Dict[str, dict] = field(default_factory=dict)
    explicit_count: int = 0
    heuristic_count: int = 0
    heuristic_matched: int = 0
    explicit_only: int = 0
    heuristic_only: int = 0
    parse_errors: int = 0


def request_api_eq(req: lifecycle.UsageRequest,
                   prices: Dict[str, Tuple[float, float, float]]) -> Optional[float]:
    p = prices.get(req.model)
    if p is None:
        return None
    unc = req.uncached_input_tokens
    cached = req.cached_input_tokens
    out = req.output_tokens
    if req.model == getattr(audit, "AUTO_REVIEW_ALIAS", "codex-auto-review") and req.input_tokens > getattr(audit, "LONG_CONTEXT_THRESHOLD_INPUT_TOKENS", 272_000):
        return (
            unc * p[0] * getattr(audit, "LONG_CONTEXT_INPUT_MULTIPLIER", 2.0)
            + cached * p[1] * getattr(audit, "LONG_CONTEXT_CACHED_MULTIPLIER", 2.0)
            + out * p[2] * getattr(audit, "LONG_CONTEXT_OUTPUT_MULTIPLIER", 1.5)
        ) / 1_000_000.0
    return (unc * p[0] + cached * p[1] + out * p[2]) / 1_000_000.0


def fmt_tokens(n: int) -> str:
    return lifecycle.fmt_tokens(n)


def fmt_eq(x: float) -> str:
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    return lifecycle.fmt_pct(x)


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def local_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def percentile(values: Sequence[float], q: float) -> float:
    vals = sorted(values)
    if not vals:
        return float("nan")
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac



def parse_aware_iso8601(value: Optional[str], flag: str) -> Optional[datetime]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{flag} must be ISO-8601, e.g. 2026-09-14T19:54:47+02:00") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{flag} must include an explicit timezone offset")
    return dt.astimezone(timezone.utc)


def in_time_window(ts: datetime, start: Optional[datetime], end: Optional[datetime]) -> bool:
    return (start is None or ts >= start) and (end is None or ts < end)


_RAW_TS_RE = re.compile(br'"timestamp"\s*:\s*"([^"]+)"')
_SHELL_TOOL_NAMES = {
    "shell_command", "exec_command", "run_command", "terminal", "bash", "shell", "command",
}
_READ_TOOL_HINTS = ("read_file", "read_text", "open_file", "file_read", "view_file", "cat_file")
_SEARCH_TOOL_HINTS = ("search", "grep", "ripgrep", "find_files", "file_search")
_WRITE_TOOL_HINTS = ("apply_patch", "write_file", "edit_file", "replace_file", "patch")
_PATH_KEY_HINTS = {
    "path", "paths", "file", "files", "filepath", "file_path", "filename", "directory", "dir",
    "root", "cwd", "workdir", "working_directory", "target_path", "source_path",
}
_PATH_SUFFIXES = {
    ".py", ".pyi", ".rs", ".go", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sh",
    ".bash", ".zsh", ".fish", ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".xml", ".html", ".css", ".scss", ".sql", ".proto", ".lock", ".csv",
}


def _raw_timestamp(raw: bytes) -> Optional[datetime]:
    match = _RAW_TS_RE.search(raw[:1024])
    if not match:
        return None
    try:
        return lifecycle.parse_ts(match.group(1).decode("utf-8", "ignore"))
    except Exception:
        return None


def _compaction_marker_from_record(raw: bytes, obj: Optional[dict] = None) -> Optional[CompactionMarker]:
    """Extract one persisted compaction marker without retaining summary/history text."""
    head = raw[:4096].lower()
    ts = _raw_timestamp(raw)
    if ts is None:
        return None
    # Legacy/current compacted records can be very large because replacement
    # history is embedded. Detect the top-level marker from the prefix and avoid
    # json.loads on those giant lines.
    if re.search(br'^\s*\{[^\n]{0,1024}"type"\s*:\s*"compacted"', head):
        return CompactionMarker(ts, "compacted")
    if obj is None:
        return None
    top_type = str(obj.get("type", "")).strip().lower()
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = str(payload.get("type", "")).strip().lower()
    if top_type == "compacted":
        return CompactionMarker(ts, "compacted")
    if payload_type == "context_compacted" and ("event" in top_type or top_type == ""):
        return CompactionMarker(ts, "context_compacted")
    # Newer rollouts can persist a first-class compaction response/turn item.
    # Treat it as a lower-level marker and dedupe it with nearby compacted /
    # context_compacted representations.
    if payload_type in {"compaction", "context_compaction"} and top_type in {
        "response_item", "turn_item", "item_completed", "event_msg", ""
    }:
        return CompactionMarker(ts, "compaction_item")
    return None


def _tool_name_and_args_generic(node: dict) -> Tuple[Optional[str], object]:
    names: List[str] = []
    if isinstance(node.get("name"), str):
        names.append(node["name"])
    if isinstance(node.get("tool_name"), str):
        names.append(node["tool_name"])
    fn = node.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        names.append(fn["name"])
    name = names[0] if names else None
    args = node.get("arguments")
    if args is None:
        args = node.get("args")
    if args is None:
        args = node.get("input")
    if args is None and isinstance(fn, dict):
        args = fn.get("arguments")
    return name, lifecycle.parse_jsonish(args)


def _generic_tool_nodes(payload: object) -> Iterable[dict]:
    seen: set[int] = set()

    def walk(obj: object, depth: int) -> Iterable[dict]:
        if depth > 9:
            return
        if isinstance(obj, dict):
            oid = id(obj)
            if oid in seen:
                return
            seen.add(oid)
            if str(obj.get("type", "")).endswith(("_output", "_result")):
                return
            name, args = _tool_name_and_args_generic(obj)
            if name and args is not None:
                yield obj
                return
            for key, value in obj.items():
                if str(key).lower() in lifecycle.NOISY_KEYS:
                    continue
                if isinstance(value, (dict, list)):
                    yield from walk(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj[:256]:
                yield from walk(value, depth + 1)

    yield from walk(payload, 0)


def _command_text(args: object) -> Optional[str]:
    args = lifecycle.parse_jsonish(args)
    if isinstance(args, dict):
        for key in ("command", "cmd", "script", "shell_command"):
            value = args.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list) and all(isinstance(x, str) for x in value[:64]):
                return " ".join(value[:64])
    elif isinstance(args, str):
        return args
    return None


def _first_shell_tokens(command: str, limit: int = 8) -> List[str]:
    if not command or len(command) > 100_000:
        return []
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except Exception:
        tokens = command.strip().split()
    return tokens[:limit]


def _classify_tool(name: str, args: object) -> str:
    low = name.strip().lower().replace("-", "_").rsplit(".", 1)[-1]
    if lifecycle.classify_action(low):
        return "lifecycle"
    if low == "exec":
        # JavaScript orchestration is opaque here. Do not guess executed nested
        # tools from source text, comments or conditional branches.
        return "opaque-wrapper"
    if any(h in low for h in _READ_TOOL_HINTS):
        return "file-read"
    if low == "rg" or any(h in low for h in _SEARCH_TOOL_HINTS):
        return "search"
    if any(h in low for h in _WRITE_TOOL_HINTS):
        return "write-edit"
    if "git" in low:
        return "git-state"
    if any(h in low for h in ("test", "pytest", "build", "compile", "lint", "check")):
        return "test-build"
    if low in _SHELL_TOOL_NAMES or "shell" in low or "exec" in low:
        command = _command_text(args) or ""
        toks = _first_shell_tokens(command)
        normalized = [t.lower() for t in toks]
        joined = " ".join(normalized[:4])
        if not normalized:
            return "shell-other"
        exe = os.path.basename(normalized[0])
        if exe == "git":
            return "git-state"
        if exe in {"rg", "grep", "egrep", "fgrep", "find", "fd", "locate"}:
            return "search"
        if exe in {"cat", "head", "tail", "less", "more"}:
            return "file-read"
        if exe in {"sed", "awk"} and not any(flag in normalized for flag in ("-i", "--in-place")):
            return "file-read"
        if exe in {"pytest", "cargo", "go", "make", "cmake", "ninja", "npm", "pnpm", "yarn", "mvn", "gradle"}:
            if exe == "cargo" and len(normalized) > 1 and normalized[1] not in {"test", "check", "build", "clippy"}:
                return "shell-other"
            if exe in {"npm", "pnpm", "yarn"} and not any(x in joined for x in ("test", "build", "lint", "check")):
                return "shell-other"
            return "test-build"
        if exe in {"python", "python3", "node", "ruby"} and any(x in joined for x in ("pytest", "test", "lint", "check")):
            return "test-build"
        return "shell-other"
    return "other"


def _normalize_resource_candidate(value: str) -> Optional[str]:
    text = value.strip().strip("'\"`;,()[]{}")
    if not text or len(text) > 1024 or text.startswith("-") or "://" in text:
        return None
    # Strip common grep/sed line suffixes while preserving Windows drive prefixes.
    if re.search(r":\d+(?::\d+)?$", text) and not re.match(r"^[A-Za-z]:[\\/]", text):
        text = re.sub(r":\d+(?::\d+)?$", "", text)
    if text in {".", "..", "/", "~"}:
        return None
    suffix = Path(text).suffix.lower()
    pathish = "/" in text or "\\" in text or text.startswith(".") or suffix in _PATH_SUFFIXES
    if not pathish:
        return None
    normalized = text.replace("\\", "/")
    return f"R-{hashlib.sha256(normalized.encode('utf-8', 'ignore')).hexdigest()[:12]}"


def _resource_hashes_from_args(args: object, tool_name: str) -> frozenset[str]:
    out: set[str] = set()
    parsed_args = lifecycle.parse_jsonish(args)

    def walk(obj: object, depth: int = 0, key_hint: str = "") -> None:
        if depth > 6 or len(out) >= 128:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                low = str(key).lower()
                if low in lifecycle.NOISY_KEYS and low not in {"command"}:
                    continue
                if low in _PATH_KEY_HINTS:
                    vals = value if isinstance(value, list) else [value]
                    for item in vals[:128]:
                        if isinstance(item, str):
                            h = _normalize_resource_candidate(item)
                            if h:
                                out.add(h)
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1, low)
        elif isinstance(obj, list):
            for value in obj[:128]:
                walk(value, depth + 1, key_hint)

    walk(parsed_args)
    command = _command_text(parsed_args)
    if command and (tool_name.lower() in _SHELL_TOOL_NAMES or "shell" in tool_name.lower() or "exec" in tool_name.lower()):
        try:
            toks = shlex.split(command, comments=False, posix=True)
        except Exception:
            toks = command.strip().split()
        for token in toks[:512]:
            h = _normalize_resource_candidate(token)
            if h:
                out.add(h)
    return frozenset(out)


def scan_raw_compaction_session(path: str, session_key: str,
                                analysis_start: Optional[datetime],
                                analysis_end: Optional[datetime]) -> RawCompactionSession:
    """Scan one rollout for explicit compaction markers, raw token usage, and tool access metadata.

    Raw token usage is intentionally retained separately from lifecycle.ParsedSession:
    the lifecycle parser deduplicates unchanged cumulative totals, while Codex can
    emit a compaction-specific last_token_usage sample whose cumulative total is
    unchanged. That sample is valuable for direct compaction-cost attribution.
    """
    out = RawCompactionSession()
    current_model = "unknown"
    current_effort = "unknown"
    seen_tools = set()
    try:
        fh = open(path, "rb")
    except OSError:
        return out
    with fh:
        for raw in fh:
            low = raw.lower()
            maybe_compaction = b'compacted' in low or b'compaction' in low
            maybe_usage = b'"token_count"' in raw or b'"token_usage_record"' in raw
            maybe_tool = b'"function_call"' in raw or b'"tool_call"' in raw or b'"custom_tool_call"' in raw or b'"tool_name"' in raw
            maybe_settings = b'"model"' in raw or b'"effort"' in raw or b'"reasoning_effort"' in raw
            if not (maybe_compaction or maybe_usage or maybe_tool or maybe_settings):
                continue

            # Giant legacy compacted records can embed the entire replacement
            # history. Detect the marker from the line prefix and skip JSON decode.
            marker = _compaction_marker_from_record(raw)
            if marker is not None and marker.kind == "compacted":
                if in_time_window(marker.ts, analysis_start, analysis_end):
                    out.markers.append(marker)
                continue

            try:
                obj = json.loads(raw)
            except Exception:
                out.parse_errors += 1
                continue
            ts = lifecycle.parse_ts(obj.get("timestamp"))
            payload = obj.get("payload")
            if ts is None or not isinstance(payload, dict):
                continue

            model = finder.model_from_payload(payload)
            if model:
                current_model = model
            effort = finder.effort_from_payload(payload)
            if effort:
                current_effort = effort

            if maybe_compaction:
                marker = _compaction_marker_from_record(raw, obj)
                if marker is not None and in_time_window(marker.ts, analysis_start, analysis_end):
                    out.markers.append(marker)

            if maybe_usage and in_time_window(ts, analysis_start, analysis_end):
                req = lifecycle.extract_usage(payload, ts, session_key, current_model, current_effort)
                if req is not None:
                    out.usage.append(RawUsageSample(ts, req, req.cumulative_total))

            if maybe_tool and in_time_window(ts, analysis_start, analysis_end):
                for node in _generic_tool_nodes(payload):
                    name, args = _tool_name_and_args_generic(node)
                    if not name:
                        continue
                    call_id = node.get("call_id") or node.get("tool_call_id")
                    identity = (str(call_id), name) if call_id else (ts, name, json.dumps(args, sort_keys=True))
                    if identity in seen_tools:
                        out.tool_duplicates += 1
                        continue
                    seen_tools.add(identity)
                    category = _classify_tool(name, args)
                    resources = _resource_hashes_from_args(args, name) if category != "opaque-wrapper" else frozenset()
                    out.tools.append(ToolAccessEvent(ts, category, resources))
    out.markers.sort(key=lambda m: (m.ts, m.kind))
    out.usage.sort(key=lambda r: r.ts)
    out.tools.sort(key=lambda t: t.ts)
    return out


def dedupe_compaction_markers(markers: Sequence[CompactionMarker], seconds: float) -> List[Tuple[datetime, datetime, Tuple[str, ...]]]:
    if not markers:
        return []
    clusters: List[List[CompactionMarker]] = []
    for marker in sorted(markers, key=lambda m: m.ts):
        if not clusters or (marker.ts - clusters[-1][-1].ts).total_seconds() > seconds:
            clusters.append([marker])
        else:
            clusters[-1].append(marker)
    out = []
    for cluster in clusters:
        kinds = tuple(sorted(set(m.kind for m in cluster)))
        out.append((cluster[0].ts, cluster[-1].ts, kinds))
    return out


def _request_identity(session_key: str, req: lifecycle.UsageRequest) -> Tuple[str, datetime, int, int, int]:
    return session_key, req.ts, req.input_tokens, req.output_tokens, req.cached_input_tokens


def _context_drop_events(parsed_session: lifecycle.ParsedSession, input_threshold: int) -> List[datetime]:
    reqs = parsed_session.usage
    out = []
    for a, b in zip(reqs, reqs[1:]):
        if a.input_tokens >= input_threshold and b.input_tokens < 0.55 * a.input_tokens:
            out.append(b.ts)
    return out


def build_compaction_audit(family: finder.Family,
                           sessions: Dict[str, finder.Session],
                           parsed: Dict[str, lifecycle.ParsedSession],
                           labels: Dict[str, str],
                           prices: Dict[str, Tuple[float, float, float]],
                           analysis_start: Optional[datetime],
                           analysis_end: Optional[datetime],
                           *,
                           dedupe_seconds: float,
                           direct_usage_seconds: float,
                           refill_fraction: float,
                           resource_lookback_minutes: float,
                           heuristic_input_threshold: int) -> CompactionAudit:
    audit_out = CompactionAudit()
    raw_by_session: Dict[str, RawCompactionSession] = {}
    clusters_by_session: Dict[str, List[Tuple[datetime, datetime, Tuple[str, ...]]]] = {}
    direct_raw_keys: set[Tuple[str, datetime, int, int, int]] = set()
    activations = trusted_spawn_activation_times(family, parsed)

    for key in family.members:
        starts = [t for t in (analysis_start, activations.get(key)) if t is not None]
        raw = scan_raw_compaction_session(sessions[key].path, key, max(starts) if starts else None, analysis_end)
        raw_by_session[key] = raw
        audit_out.parse_errors += raw.parse_errors
        counts = Counter(t.category for t in raw.tools)
        audit_out.tool_activity[labels[key]] = {
            "role": lifecycle.role_for_session(sessions[key]), "categories": dict(counts),
            "observed_events": len(raw.tools), "duplicates_removed": raw.tool_duplicates,
            "unclassified_or_opaque": sum(counts[c] for c in ("other", "shell-other", "opaque-wrapper")),
            "parse_errors": raw.parse_errors,
        }
        clusters_by_session[key] = dedupe_compaction_markers(raw.markers, dedupe_seconds)

    event_index = 0
    for key in family.members:
        clusters = clusters_by_session[key]
        if not clusters:
            continue
        reqs = parsed[key].usage
        raw_usage = raw_by_session[key].usage
        tools = raw_by_session[key].tools
        role = lifecycle.role_for_key(key, family, sessions)
        label = labels.get(key, key)
        for idx, (start_ts, marker_end, kinds) in enumerate(clusters):
            event_index += 1
            next_compaction = clusters[idx + 1][0] if idx + 1 < len(clusters) else None
            event = CompactionEvent(
                index=event_index,
                session_key=key,
                agent_label=label,
                role=role,
                ts=start_ts,
                completion_ts=marker_end,
                marker_kinds=kinds,
            )

            # Direct usage: strongest evidence is a raw token_count emitted inside
            # the compacted/context_compacted marker pair. If the pair is absent,
            # allow only a very-near sample; never grab the next normal turn several
            # seconds later just to fill the field.
            candidates = [u for u in raw_usage if start_ts <= u.ts <= marker_end]
            method = "between-compaction-markers"
            confidence = "high"
            if not candidates:
                upper = marker_end + timedelta(seconds=max(0.0, direct_usage_seconds))
                candidates = [u for u in raw_usage if marker_end < u.ts <= upper]
                method = "near-marker"
                confidence = "medium"
            if candidates:
                chosen = min(candidates, key=lambda u: abs((u.ts - marker_end).total_seconds()))
                event.direct_usage = chosen.request
                event.direct_api_eq = request_api_eq(chosen.request, prices)
                event.direct_usage_method = method
                event.direct_usage_confidence = confidence
                ident = _request_identity(key, chosen.request)
                event.direct_usage_in_primary_totals = any(_request_identity(key, r) == ident for r in reqs)
                audit_out.direct_matched += 1
                audit_out.direct_totals.add_request(chosen.request, prices)
                if event.direct_usage_in_primary_totals:
                    audit_out.direct_in_primary_count += 1
                else:
                    audit_out.direct_outside_primary_totals.add_request(chosen.request, prices)
                direct_raw_keys.add(_request_identity(key, chosen.request))

            before_req = next((r for r in reversed(reqs) if r.ts < start_ts), None)
            event.before_input_tokens = before_req.input_tokens if before_req else None

            # ParsedSession is preferable for recovery because duplicate cumulative
            # token_count UI samples are already removed. Exclude any request that
            # was itself matched as direct compaction usage.
            post_reqs = [
                r for r in reqs
                if r.ts > marker_end and _request_identity(key, r) not in direct_raw_keys
            ]
            if next_compaction is not None:
                post_reqs = [r for r in post_reqs if r.ts < next_compaction]

            refill_target = None
            if event.before_input_tokens and event.before_input_tokens > 0:
                refill_target = max(1, int(event.before_input_tokens * refill_fraction))
            event.refill_target_tokens = refill_target

            recovery_reqs: List[lifecycle.UsageRequest] = []
            recovery_end = next_compaction
            recovery_reason = "next-compaction" if next_compaction is not None else "session-end"
            if next_compaction is None:
                recovery_end = min(
                    [x for x in (analysis_end, sessions[key].last_ts) if x is not None],
                    default=marker_end,
                )
            for req in post_reqs:
                recovery_reqs.append(req)
                if refill_target is not None and req.input_tokens >= refill_target:
                    recovery_end = req.ts
                    recovery_reason = "refill-threshold"
                    event.refill_reached = True
                    break
            if recovery_reqs:
                event.after_input_tokens = recovery_reqs[0].input_tokens
                event.recovery_peak_input_tokens = max(r.input_tokens for r in recovery_reqs)
            if event.before_input_tokens and event.after_input_tokens is not None:
                event.shrink_fraction = 1.0 - (event.after_input_tokens / event.before_input_tokens)

            if recovery_end is None:
                recovery_end = marker_end
            event.recovery_end = recovery_end
            event.recovery_end_reason = recovery_reason
            event.recovery_duration_seconds = max(0.0, (recovery_end - marker_end).total_seconds())
            event.recovery_totals = aggregate_requests(recovery_reqs, prices)
            audit_out.recovery_totals.add_totals(event.recovery_totals)
            for req in recovery_reqs:
                audit_out.recovery_request_keys.add(_request_identity(key, req))

            lookback_start = start_ts - timedelta(minutes=max(0.0, resource_lookback_minutes))
            pre_tools = [t for t in tools if lookback_start <= t.ts < start_ts]
            recovery_tools = [t for t in tools if marker_end < t.ts <= recovery_end]
            pre_resources = set().union(*(set(t.resource_hashes) for t in pre_tools)) if pre_tools else set()
            post_resources = set().union(*(set(t.resource_hashes) for t in recovery_tools)) if recovery_tools else set()
            repeated = pre_resources & post_resources
            event.pre_resources = len(pre_resources)
            event.unique_resources_after = len(post_resources)
            event.repeated_resources = len(repeated)
            event.tool_events = len(recovery_tools)
            event.tool_counts.update(t.category for t in recovery_tools)
            event.repeated_resource_access_events = sum(bool(set(t.resource_hashes) & pre_resources) for t in recovery_tools)
            pre_read_resources = set().union(*(set(t.resource_hashes) for t in pre_tools if t.category == "file-read")) if pre_tools else set()
            post_read_resources = set().union(*(set(t.resource_hashes) for t in recovery_tools if t.category == "file-read")) if recovery_tools else set()
            repeated_reads = pre_read_resources & post_read_resources
            event.repeated_read_resources = len(repeated_reads)
            event.repeated_read_events = sum(
                bool(set(t.resource_hashes) & pre_read_resources)
                for t in recovery_tools if t.category == "file-read"
            )
            audit_out.recovery_tool_events += len(recovery_tools)
            audit_out.events.append(event)

    audit_out.events.sort(key=lambda e: (e.ts, e.session_key, e.index))
    for i, event in enumerate(audit_out.events, 1):
        event.index = i
    audit_out.explicit_count = len(audit_out.events)

    # Same-session non-recovery baseline. This is intentionally descriptive,
    # not a causal counterfactual: it asks whether recovery requests look more
    # expensive than the rest of the same sessions.
    sessions_with_compaction = {e.session_key for e in audit_out.events}
    recovery_intervals: Dict[str, List[Tuple[datetime, datetime]]] = defaultdict(list)
    for e in audit_out.events:
        if e.recovery_end is not None:
            recovery_intervals[e.session_key].append((e.completion_ts, e.recovery_end))
    baseline_reqs: List[lifecycle.UsageRequest] = []
    for key in sessions_with_compaction:
        compaction_times = [e.ts for e in audit_out.events if e.session_key == key]
        for req in parsed[key].usage:
            ident = _request_identity(key, req)
            if ident in audit_out.recovery_request_keys or ident in direct_raw_keys:
                continue
            if any(abs((req.ts - ts).total_seconds()) <= max(2.0, direct_usage_seconds) for ts in compaction_times):
                continue
            baseline_reqs.append(req)
        intervals = recovery_intervals.get(key, [])
        for tool in raw_by_session[key].tools:
            if not any(a < tool.ts <= b for a, b in intervals):
                audit_out.baseline_tool_events += 1
    audit_out.baseline_totals = aggregate_requests(baseline_reqs, prices)

    rr = audit_out.recovery_totals.requests
    br = audit_out.baseline_totals.requests
    baseline_api_per_req = audit_out.baseline_totals.api_eq / br if br else None
    recovery_api_per_req = audit_out.recovery_totals.api_eq / rr if rr else None
    baseline_input_per_req = audit_out.baseline_totals.input_tokens / br if br else None
    recovery_input_per_req = audit_out.recovery_totals.input_tokens / rr if rr else None
    baseline_uncached_per_req = audit_out.baseline_totals.uncached_input_tokens / br if br else None
    recovery_uncached_per_req = audit_out.recovery_totals.uncached_input_tokens / rr if rr else None
    baseline_tools_per_req = audit_out.baseline_tool_events / br if br else None
    recovery_tools_per_req = audit_out.recovery_tool_events / rr if rr else None
    api_delta = None
    input_delta = None
    if rr and baseline_api_per_req is not None:
        api_delta = audit_out.recovery_totals.api_eq - rr * baseline_api_per_req
    if rr and baseline_input_per_req is not None:
        input_delta = audit_out.recovery_totals.input_tokens - rr * baseline_input_per_req
    audit_out.baseline = {
        "recovery_requests": rr,
        "baseline_requests": br,
        "recovery_api_eq_per_request": recovery_api_per_req,
        "baseline_api_eq_per_request": baseline_api_per_req,
        "recovery_input_per_request": recovery_input_per_req,
        "baseline_input_per_request": baseline_input_per_req,
        "recovery_uncached_input_per_request": recovery_uncached_per_req,
        "baseline_uncached_input_per_request": baseline_uncached_per_req,
        "recovery_tool_events_per_request": recovery_tools_per_req,
        "baseline_tool_events_per_request": baseline_tools_per_req,
        "api_eq_delta_vs_same_session_baseline": api_delta,
        "input_token_delta_vs_same_session_baseline": input_delta,
    }

    # Role aggregation and frequency.
    role_events: Dict[str, List[CompactionEvent]] = defaultdict(list)
    for event in audit_out.events:
        role_events[event.role].append(event)
    by_role: Dict[str, dict] = {}
    for role, events in role_events.items():
        direct = TokenTotals()
        recovery = TokenTotals()
        matched = 0
        shrink_values = []
        repeated_read_events = 0
        gaps = []
        per_session: Dict[str, List[CompactionEvent]] = defaultdict(list)
        for e in events:
            per_session[e.session_key].append(e)
            if e.direct_usage is not None:
                direct.add_request(e.direct_usage, prices)
                matched += 1
            recovery.add_totals(e.recovery_totals)
            if e.shrink_fraction is not None:
                shrink_values.append(e.shrink_fraction)
            repeated_read_events += e.repeated_read_events
        runtime_seconds = 0.0
        for key, evs in per_session.items():
            evs.sort(key=lambda e: e.ts)
            gaps.extend((b.ts - a.ts).total_seconds() for a, b in zip(evs, evs[1:]))
            session_reqs = parsed[key].usage
            if len(session_reqs) >= 2:
                runtime_seconds += max(0.0, (session_reqs[-1].ts - session_reqs[0].ts).total_seconds())
        by_role[role] = {
            "compactions": len(events),
            "direct_matched": matched,
            "direct": direct,
            "recovery": recovery,
            "runtime_hours": runtime_seconds / 3600.0,
            "compactions_per_hour": len(events) / (runtime_seconds / 3600.0) if runtime_seconds > 0 else None,
            "median_gap_seconds": statistics.median(gaps) if gaps else None,
            "median_shrink_fraction": statistics.median(shrink_values) if shrink_values else None,
            "repeated_read_events": repeated_read_events,
        }
    audit_out.by_role = by_role

    # Validate the old context-drop heuristic against explicit events.
    explicit_times: List[Tuple[str, datetime]] = [(e.session_key, e.ts) for e in audit_out.events]
    heuristic_times: List[Tuple[str, datetime]] = []
    for key in family.members:
        heuristic_times.extend((key, ts) for ts in _context_drop_events(parsed[key], heuristic_input_threshold))
    used_explicit: set[int] = set()
    matched = 0
    for h_key, h_ts in heuristic_times:
        candidates = [
            (i, abs((e_ts - h_ts).total_seconds()))
            for i, (e_key, e_ts) in enumerate(explicit_times)
            if e_key == h_key and i not in used_explicit and abs((e_ts - h_ts).total_seconds()) <= 120
        ]
        if candidates:
            i, _ = min(candidates, key=lambda x: x[1])
            used_explicit.add(i)
            matched += 1
    audit_out.heuristic_count = len(heuristic_times)
    audit_out.heuristic_matched = matched
    audit_out.explicit_only = max(0, len(explicit_times) - matched)
    audit_out.heuristic_only = max(0, len(heuristic_times) - matched)
    return audit_out


def parsed_activity_times(ps: lifecycle.ParsedSession,
                          start: Optional[datetime] = None,
                          end: Optional[datetime] = None) -> List[datetime]:
    """Observed inference/lifecycle timestamps, optionally restricted to a window."""
    rows = [r.ts for r in ps.usage if in_time_window(r.ts, start, end)]
    rows.extend(a.ts for a in ps.actions if in_time_window(a.ts, start, end))
    rows.sort()
    return rows


def trusted_spawn_activation_times(family: finder.Family,
                                   parsed: Dict[str, lifecycle.ParsedSession]) -> Dict[str, datetime]:
    """Earliest trusted spawn time for each child in this family.

    A forked/resumed rollout can contain inherited timestamps older than the point
    where that child was actually activated. Trusted spawn time is therefore the
    preferred child activation boundary for cutoff reconstruction.
    """
    members = set(family.members)
    out: Dict[str, datetime] = {}
    for caller in family.members:
        for action in parsed[caller].actions:
            child = action.matched_session
            if (action.kind != "spawn" or not lifecycle.is_trusted_action(action)
                    or not child or child not in members):
                continue
            if child not in out or action.ts < out[child]:
                out[child] = action.ts
    return out


def effective_session_activation(key: str,
                                 sessions: Dict[str, finder.Session],
                                 spawn_activation: Dict[str, datetime]) -> Optional[datetime]:
    return spawn_activation.get(key) or sessions[key].first_ts


def effective_session_last_activity(key: str,
                                    sessions: Dict[str, finder.Session],
                                    parsed: Dict[str, lifecycle.ParsedSession]) -> Optional[datetime]:
    times = parsed_activity_times(parsed[key])
    candidates = []
    if times:
        candidates.append(times[-1])
    if sessions[key].last_ts is not None:
        candidates.append(sessions[key].last_ts)
    return max(candidates) if candidates else None


def classify_sessions_for_window(family: finder.Family,
                                 sessions: Dict[str, finder.Session],
                                 parsed: Dict[str, lifecycle.ParsedSession],
                                 start: Optional[datetime],
                                 end: Optional[datetime]) -> AnalysisMeta:
    meta = AnalysisMeta(
        requested_after=start,
        requested_before=end,
        source_family_start=family.first_ts(sessions),
        source_family_end=family.last_ts(sessions),
        original_family_root=family.root,
    )
    if start is None and end is None:
        meta.in_window = list(family.members)
        return meta

    spawn_activation = trusted_spawn_activation_times(family, parsed)
    for key in family.members:
        activation = effective_session_activation(key, sessions, spawn_activation)
        last = effective_session_last_activity(key, sessions, parsed)
        if activation is None and last is None:
            meta.post_window.append(key)
            continue
        activation = activation or last
        last = last or activation
        assert activation is not None and last is not None

        # A trusted post-cutoff spawn overrides inherited rollout history.
        if start is not None and last < start:
            meta.pre_window.append(key)
            continue
        if end is not None and activation >= end:
            meta.post_window.append(key)
            continue
        if start is not None and activation < start <= last:
            meta.carry_in.append(key)
        elif (start is None or last >= start) and (end is None or activation < end):
            meta.in_window.append(key)
        else:
            meta.post_window.append(key)
        if end is not None and activation < end <= last:
            meta.carry_out.append(key)
    return meta


def select_analysis_root(family: finder.Family,
                         sessions: Dict[str, finder.Session],
                         parsed: Dict[str, lifecycle.ParsedSession],
                         start: Optional[datetime],
                         end: Optional[datetime],
                         override: Optional[str]) -> Tuple[Optional[str], str, str, List[dict]]:
    if override:
        key = family.root if override == "ROOT" else lifecycle.resolve_session(override, sessions)
        if key not in family.members:
            raise ValueError("--analysis-root must uniquely identify a member of the selected family")
        return key, "explicit-override", "high", []
    if start is None and end is None:
        return family.root, "family-root", "high", []

    # Root selection is structural, not a role-name or busiest-agent contest.
    # An idle parent can still own active descendants; recorded session age does
    # not disqualify a resumed parent. Multiple active roots require an override.
    members = set(family.members)
    incoming = {e.child for e in family.edges if e.parent in members and e.child in members}
    for key in family.members:
        incoming.update(a.matched_session for a in parsed[key].actions
                        if a.kind == "spawn" and lifecycle.is_trusted_action(a)
                        and a.matched_session in members)
    candidates = []
    for key in sorted(members - incoming):
        descendants = graph_descendants(family, key) | trusted_spawn_descendants(family, parsed, key)
        activity = sorted(t for member in ({key} | descendants)
                          for t in parsed_activity_times(parsed[member], start, end))
        if activity:
            candidates.append({"session_key": key, "first_window_activity": activity[0],
                               "last_window_activity": activity[-1],
                               "activity_events": len(activity)})
    if not candidates:
        return None, "no-structural-root-with-window-activity", "none", candidates
    if len(candidates) != 1:
        return None, "ambiguous-active-structural-roots", "low", candidates
    confidence = "medium" if any(e.confidence != "high" for e in family.edges) else "high"
    return candidates[0]["session_key"], "structural-root-with-window-activity", confidence, candidates

def graph_descendants(family: finder.Family, root: str) -> set[str]:
    children: Dict[str, set[str]] = defaultdict(set)
    for edge in family.edges:
        children[edge.parent].add(edge.child)
    out: set[str] = set()
    stack = list(children.get(root, set()))
    while stack:
        key = stack.pop()
        if key in out:
            continue
        out.add(key)
        stack.extend(children.get(key, set()))
    return out


def trusted_spawn_descendants(family: finder.Family,
                              parsed: Dict[str, lifecycle.ParsedSession],
                              root: str) -> set[str]:
    children: Dict[str, set[str]] = defaultdict(set)
    members = set(family.members)
    for key in family.members:
        for action in parsed[key].actions:
            if action.kind == "spawn" and lifecycle.is_trusted_action(action) and action.matched_session in members:
                children[key].add(action.matched_session)
    out: set[str] = set()
    stack = list(children.get(root, set()))
    while stack:
        key = stack.pop()
        if key in out:
            continue
        out.add(key)
        stack.extend(children.get(key, set()))
    return out


def build_analysis_family(full_family: finder.Family,
                          sessions: Dict[str, finder.Session],
                          parsed: Dict[str, lifecycle.ParsedSession],
                          meta: AnalysisMeta,
                          analysis_root: str) -> finder.Family:
    start, end = meta.requested_after, meta.requested_before
    if not meta.windowed and analysis_root == full_family.root:
        meta.analysis_root = analysis_root
        meta.membership_method = "full-family"
        meta.analysis_start = full_family.first_ts(sessions)
        last = full_family.last_ts(sessions)
        meta.analysis_end = last + timedelta(microseconds=1) if last else None
        return full_family

    graph = graph_descendants(full_family, analysis_root)
    trusted = trusted_spawn_descendants(full_family, parsed, analysis_root)
    ancestry = graph | trusted
    spawn_activation = trusted_spawn_activation_times(full_family, parsed)

    def has_window_activity(key: str) -> bool:
        return bool(parsed_activity_times(parsed[key], start, end))

    def eligible(key: str) -> bool:
        if key == analysis_root:
            return True
        # Non-root carry-ins remain contamination and are reported separately.
        # The selected orchestrator is the one allowed carry-in exception.
        if key in meta.carry_in:
            return False
        activation = effective_session_activation(key, sessions, spawn_activation)
        spawned_in_window = activation is not None and in_time_window(activation, start, end)
        return spawned_in_window or has_window_activity(key)

    members = [analysis_root] + sorted(k for k in ancestry if k != analysis_root and eligible(k))
    meta.membership_method = "selected-root-descendants-by-activity"
    members = list(dict.fromkeys(members))
    meta.analysis_root = analysis_root
    meta.excluded_in_window = [k for k in meta.in_window if k not in members]
    view = finder.Family(
        members=members,
        root=analysis_root,
        edges=[e for e in full_family.edges if e.parent in members and e.child in members],
        family_key=full_family.family_key,
        score=full_family.score,
        sample_quality=full_family.sample_quality,
    )

    activity = []
    for key in members:
        activity.extend(parsed_activity_times(parsed[key], start, end))
    activity.sort()
    if start is not None:
        meta.analysis_start = start
    elif activity:
        meta.analysis_start = activity[0]
    else:
        meta.analysis_start = view.first_ts(sessions)
    if end is not None:
        meta.analysis_end = end
    elif activity:
        meta.analysis_end = activity[-1] + timedelta(microseconds=1)
    else:
        last = view.last_ts(sessions)
        meta.analysis_end = last + timedelta(microseconds=1) if last else None
    return view

def filter_parsed_for_window(family: finder.Family,
                             parsed: Dict[str, lifecycle.ParsedSession],
                             start: Optional[datetime],
                             end: Optional[datetime]) -> Dict[str, lifecycle.ParsedSession]:
    out: Dict[str, lifecycle.ParsedSession] = {}
    for key in family.members:
        src = parsed[key]
        dst = lifecycle.ParsedSession()
        dst.usage = [r for r in src.usage if in_time_window(r.ts, start, end)]
        dst.actions = [a for a in src.actions if in_time_window(a.ts, start, end)]
        dst.action_outputs = src.action_outputs
        dst.parse_errors = src.parse_errors
        dst.duplicate_actions_removed = src.duplicate_actions_removed
        out[key] = dst
    return out


def _safe_digest(value: str, prefix: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8', 'ignore')).hexdigest()[:12]}"


def structured_assignment_from_task_name(value: object, roles: Sequence[str]) -> Optional[Tuple[str, str, str]]:
    """Parse explicit versioned or legacy labels; never guess slugged task names."""
    a = attribution.parse_label(value, roles)
    if a is None:
        return None
    unit = json.dumps([a.run, a.scope, a.unit])
    return _safe_digest(unit, "CK"), _safe_digest(json.dumps([unit, a.role, a.attempt]), "AS"), a.role


def _find_task_name(obj: object, depth: int = 0) -> Optional[str]:
    if depth > 5:
        return None
    obj = lifecycle.parse_jsonish(obj)
    if isinstance(obj, dict):
        value = obj.get("task_name")
        if isinstance(value, str):
            return value
        for key, value in obj.items():
            if str(key).lower() in lifecycle.NOISY_KEYS:
                continue
            if isinstance(value, (dict, list)):
                found = _find_task_name(value, depth + 1)
                if found is not None:
                    return found
    elif isinstance(obj, list):
        for value in obj[:64]:
            found = _find_task_name(value, depth + 1)
            if found is not None:
                return found
    return None


def build_spawn_assignment_fingerprints(family: finder.Family,
                                         sessions: Dict[str, finder.Session],
                                         parsed: Dict[str, lifecycle.ParsedSession],
                                         roles: Sequence[str],
                                         start: Optional[datetime],
                                         end: Optional[datetime]) -> Dict[str, AssignmentFingerprint]:
    """Read only compact structured spawn task labels; raw values are never retained/exported."""
    out: Dict[str, AssignmentFingerprint] = {}
    trusted_spawn_calls = {
        a.call_id for key in family.members for a in parsed[key].actions
        if a.kind == "spawn" and a.call_id and lifecycle.is_trusted_action(a) and a.matched_session
    }
    if not trusted_spawn_calls:
        return out
    for key in family.members:
        path = sessions[key].path
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                if b'spawn_agent' not in raw.lower() and b'create_agent' not in raw.lower():
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ts = lifecycle.parse_ts(obj.get("timestamp"))
                if ts is None or not in_time_window(ts, start, end):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                for node in lifecycle.action_node_candidates(payload):
                    _, kind, args, call_raw = action_name_and_args(node)
                    if kind != "spawn" or call_raw is None:
                        continue
                    call_id = lifecycle.hash_id(call_raw)
                    if not call_id or call_id not in trusted_spawn_calls:
                        continue
                    task_name = _find_task_name(args)
                    parsed_label = structured_assignment_from_task_name(task_name, roles)
                    if parsed_label is None:
                        continue
                    chunk_key, assignment_key, role = parsed_label
                    out[call_id] = AssignmentFingerprint(
                        call_id=call_id,
                        chunk_key=chunk_key,
                        assignment_key=assignment_key,
                        role=role,
                        source="spawn-task-name",
                        confidence="high",
                    )
    # Result-side fallback: some runtimes repeat task_name only in the spawn result.
    missing = trusted_spawn_calls - set(out)
    if missing:
        for key in family.members:
            path = sessions[key].path
            try:
                fh = open(path, "rb")
            except OSError:
                continue
            with fh:
                for raw in fh:
                    if b'"call_id"' not in raw and b'"tool_call_id"' not in raw and b'"function_call_id"' not in raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    ts = lifecycle.parse_ts(obj.get("timestamp"))
                    if ts is None or not in_time_window(ts, start, end):
                        continue
                    rec = output_record_raw(obj.get("payload"))
                    if not rec:
                        continue
                    call_id, result = rec
                    if call_id not in missing:
                        continue
                    task_name = _find_task_name(result)
                    parsed_label = structured_assignment_from_task_name(task_name, roles)
                    if parsed_label is None:
                        continue
                    chunk_key, assignment_key, role = parsed_label
                    out[call_id] = AssignmentFingerprint(
                        call_id=call_id,
                        chunk_key=chunk_key,
                        assignment_key=assignment_key,
                        role=role,
                        source="spawn-result-task-name",
                        confidence="high",
                    )
                    missing.discard(call_id)
                    if not missing:
                        break
            if not missing:
                break
    return out


def annotate_windows_with_assignments(windows: Sequence[ActiveWindow],
                                      family: finder.Family,
                                      parsed: Dict[str, lifecycle.ParsedSession],
                                      fingerprints: Dict[str, AssignmentFingerprint]) -> Dict[str, str]:
    by_target: Dict[str, AssignmentFingerprint] = {}
    for key in family.members:
        for action in parsed[key].actions:
            if action.kind != "spawn" or not action.matched_session or not action.call_id:
                continue
            fp = fingerprints.get(action.call_id)
            if fp is not None and fp.role == next((w.role for w in windows if w.target_key == action.matched_session), fp.role):
                by_target[action.matched_session] = fp
    chunk_first: Dict[str, datetime] = {}
    for w in windows:
        fp = by_target.get(w.target_key)
        if fp is None:
            continue
        w.chunk_key = fp.chunk_key
        w.assignment_key = fp.assignment_key
        w.assignment_source = fp.source
        w.assignment_confidence = fp.confidence
        chunk_first[fp.chunk_key] = min(chunk_first.get(fp.chunk_key, w.spawn_ts), w.spawn_ts)
    ordered = sorted(chunk_first, key=lambda ck: (chunk_first[ck], ck))
    labels = {ck: f"C{i:02d}" for i, ck in enumerate(ordered, 1)}
    for w in windows:
        if w.chunk_key in labels:
            w.chunk_label = labels[w.chunk_key]
    return labels


def carry_in_activity(meta: AnalysisMeta,
                      parsed_full: Dict[str, lifecycle.ParsedSession],
                      prices: Dict[str, Tuple[float, float, float]]) -> TokenTotals:
    if not meta.carry_in:
        return TokenTotals()
    start, end = meta.requested_after, meta.requested_before
    reqs = []
    for key in meta.carry_in:
        if key == meta.analysis_root:
            continue
        reqs.extend(r for r in parsed_full[key].usage if in_time_window(r.ts, start, end))
    return aggregate_requests(reqs, prices)


def aggregate_requests(reqs: Iterable[lifecycle.UsageRequest],
                       prices: Dict[str, Tuple[float, float, float]]) -> TokenTotals:
    t = TokenTotals()
    for req in reqs:
        t.add_request(req, prices)
    return t


def token_summary(reqs, prices) -> dict:
    return totals_dict(aggregate_requests(reqs, prices))


def totals_dict(t: TokenTotals) -> dict:
    return {
        "requests": t.requests, "input_tokens": t.input_tokens,
        "cached_input_tokens": t.cached_input_tokens, "uncached_input_tokens": t.uncached_input_tokens,
        "output_tokens": t.output_tokens, "reasoning_output_tokens": t.reasoning_tokens,
        "total_tokens": t.total_tokens, "api_list_equivalent_usd": round(t.api_eq, 6),
        "price_coverage": t.price_coverage if math.isfinite(t.price_coverage) else None,
    }


def apply_assignment_windows(windows, identities, family_key):
    unit_labels = {}
    for window in windows:
        identity = identities[window.target_key]
        assignment = identity["assignment"]
        if assignment is None or identity["issues"] or assignment.scope != "chunk":
            continue
        # Namespace by the source family, not a temporary time-window root.
        unit = json.dumps([family_key, assignment.run, assignment.scope, assignment.unit])
        window.chunk_key = _safe_digest(unit, "CK")
        window.assignment_key = _safe_digest(json.dumps([unit, identity["parent"], assignment.role, assignment.attempt]), "AS")
        window.assignment_source = identity["assignment_source"]
        window.assignment_confidence = "declared" if identity["assignment_source"] == "declared-map" else "high"
        window.chunk_label = unit_labels.setdefault(window.chunk_key, f"C{len(unit_labels) + 1:02d}")


def print_nested_report(nested):
    print("\nNested responsibility accounting")
    print("--------------------------------")
    print("agent  parent role                  unit direct requests   direct tokens  inclusive subtree")
    for row in nested["sessions"]:
        print(f"{row['agent']:<6} {(row['parent'] or '-'):<6} {row['role']:<21} "
              f"{(row['unit'] or '-'):<4} {row['direct']['requests']:>15,} "
              f"{row['direct']['total_tokens']:>15,} {row['inclusive_subtree']['total_tokens']:>18,}")
    print("Unit buckets (additive; groups are not silently split into chunks):")
    for row in nested["units"]:
        print(f"  {row['unit']} {row['scope']}: {row['attributed']['requests']:,} requests; {row['attributed']['total_tokens']:,} tokens")
    print(f"Unattributed: {nested['unattributed']['total_tokens']:,} tokens")
    print("Coverage:", json.dumps(nested["coverage"], sort_keys=True))
    print(nested["note"])


def all_family_requests(family: finder.Family,
                        parsed: Dict[str, lifecycle.ParsedSession]) -> List[lifecycle.UsageRequest]:
    rows: List[lifecycle.UsageRequest] = []
    for key in family.members:
        rows.extend(parsed[key].usage)
    rows.sort(key=lambda r: r.ts)
    return rows


def trusted_spawn_actions(family: finder.Family,
                          parsed: Dict[str, lifecycle.ParsedSession]) -> List[lifecycle.ActionEvent]:
    rows = []
    for key in family.members:
        for action in parsed[key].actions:
            if (action.kind == "spawn" and lifecycle.is_trusted_action(action) and action.matched_session
                    and action.matched_session in family.members):
                rows.append(action)
    rows.sort(key=lambda a: (a.ts, a.caller_key, a.call_id or ""))
    return rows


def build_active_windows(family: finder.Family,
                         sessions: Dict[str, finder.Session],
                         parsed: Dict[str, lifecycle.ParsedSession],
                         labels: Dict[str, str],
                         stage_roles: Optional[set[str]],
                         tail_seconds: float,
                         analysis_start: Optional[datetime] = None,
                         analysis_end: Optional[datetime] = None) -> Tuple[List[ActiveWindow], List[str]]:
    """Build observed descendant lifetime windows from trusted spawns.

    Unlike v1's spawn-defined stages, one spawn never truncates another active
    child. This preserves overlaps such as implementer + validator or multiple
    implementers. The default tail is zero so the state represents observed
    child-session lifetime rather than a guessed grace period.
    """
    windows: List[ActiveWindow] = []
    recognized_targets: set[str] = set()

    for action in trusted_spawn_actions(family, parsed):
        key = action.matched_session
        assert key is not None
        role = lifecycle.role_for_key(key, family, sessions)
        if stage_roles is not None and role not in stage_roles:
            continue
        child = sessions[key]
        # Trusted spawn time is the effective activation boundary. A forked
        # rollout may contain inherited history with much older timestamps.
        start = action.ts
        natural_end = child.last_ts or start
        end = natural_end + timedelta(seconds=max(0.0, tail_seconds))
        if analysis_start is not None:
            start = max(start, analysis_start)
        if analysis_end is not None:
            end = min(end, analysis_end)
        if end < start:
            end = start
        windows.append(ActiveWindow(
            index=0,
            role=role,
            target_key=key,
            target_label=labels[key],
            spawn_ts=action.ts,
            start=start,
            end=end,
            spawn_method=action.match_method,
            spawn_confidence=action.match_confidence,
        ))
        recognized_targets.add(key)

    windows.sort(key=lambda w: (w.start, w.end, w.target_key))
    for i, w in enumerate(windows, 1):
        w.index = i

    role_children = [
        k for k in family.members
        if k != family.root and (stage_roles is None or lifecycle.role_for_key(k, family, sessions) in stage_roles)
    ]
    excluded = [k for k in role_children if k not in recognized_targets]
    return windows, excluded


def active_windows_for_ts(ts: datetime, windows: Sequence[ActiveWindow]) -> List[ActiveWindow]:
    return [w for w in windows if w.start <= ts < w.end]


def _plural_role(role: str) -> str:
    if role.endswith("y") and not role.endswith(("ay", "ey", "oy", "uy")):
        return role[:-1] + "ies"
    if role.endswith("s"):
        return role
    return role + "s"


def active_state_label(ts: datetime, windows: Sequence[ActiveWindow]) -> str:
    active = active_windows_for_ts(ts, windows)
    if not active:
        return "no-recognized-child-active"
    counts = Counter(w.role for w in active)
    parts = []
    for role in sorted(counts):
        n = counts[role]
        if n == 1:
            parts.append(role)
        else:
            parts.append(f"multiple {_plural_role(role)}")
    if len(parts) == 1 and not parts[0].startswith("multiple "):
        return parts[0] + " only"
    return "+".join(parts)


def root_active_state_costs(family: finder.Family,
                            parsed: Dict[str, lifecycle.ParsedSession],
                            windows: Sequence[ActiveWindow],
                            prices: Dict[str, Tuple[float, float, float]]) -> Dict[str, TokenTotals]:
    out: Dict[str, TokenTotals] = defaultdict(TokenTotals)
    for req in parsed[family.root].usage:
        out[active_state_label(req.ts, windows)].add_request(req, prices)
    return out

def child_count_bucket(n: int) -> str:
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n == 2:
        return "2"
    return "3+"


def root_child_count_costs(family: finder.Family,
                           parsed: Dict[str, lifecycle.ParsedSession],
                           windows: Sequence[ActiveWindow],
                           prices: Dict[str, Tuple[float, float, float]]) -> Dict[str, TokenTotals]:
    out: Dict[str, TokenTotals] = defaultdict(TokenTotals)
    for req in parsed[family.root].usage:
        n = len(active_windows_for_ts(req.ts, windows))
        out[child_count_bucket(n)].add_request(req, prices)
    return out


def _merged_interval_seconds(intervals: Sequence[Tuple[datetime, datetime]]) -> float:
    rows = sorted((a, b) for a, b in intervals if b > a)
    if not rows:
        return 0.0
    total = 0.0
    cur_a, cur_b = rows[0]
    for a, b in rows[1:]:
        if a <= cur_b:
            if b > cur_b:
                cur_b = b
        else:
            total += (cur_b - cur_a).total_seconds()
            cur_a, cur_b = a, b
    total += (cur_b - cur_a).total_seconds()
    return total


def concurrency_metrics(windows: Sequence[ActiveWindow],
                        span_start: Optional[datetime],
                        span_end: Optional[datetime]) -> dict:
    if not span_start or not span_end or span_end <= span_start:
        return {
            "agent_seconds": 0.0,
            "active_wall_seconds": 0.0,
            "overlap_wall_seconds": 0.0,
            "extra_concurrency_seconds": 0.0,
            "peak_concurrent_children": 0,
            "wall_seconds_by_count": {},
        }
    points = {span_start, span_end}
    for w in windows:
        points.add(max(span_start, min(span_end, w.start)))
        points.add(max(span_start, min(span_end, w.end)))
    ordered = sorted(points)
    by_count = defaultdict(float)
    agent_seconds = 0.0
    active_wall = 0.0
    overlap_wall = 0.0
    extra = 0.0
    peak = 0
    for a, b in zip(ordered, ordered[1:]):
        if b <= a:
            continue
        mid = a + (b - a) / 2
        n = len(active_windows_for_ts(mid, windows))
        sec = (b - a).total_seconds()
        bucket = child_count_bucket(n)
        by_count[bucket] += sec
        agent_seconds += sec * n
        if n >= 1:
            active_wall += sec
        if n >= 2:
            overlap_wall += sec
            extra += sec * (n - 1)
        peak = max(peak, n)
    return {
        "agent_seconds": agent_seconds,
        "active_wall_seconds": active_wall,
        "overlap_wall_seconds": overlap_wall,
        "extra_concurrency_seconds": extra,
        "peak_concurrent_children": peak,
        "wall_seconds_by_count": dict(by_count),
    }


def agent_overlap_rows(windows: Sequence[ActiveWindow]) -> List[dict]:
    rows = []
    for w in windows:
        intersections = []
        others = set()
        for other in windows:
            if other is w:
                continue
            a = max(w.start, other.start)
            b = min(w.end, other.end)
            if b > a:
                intersections.append((a, b))
                others.add(f"{other.target_label}({other.role})")
        overlap = _merged_interval_seconds(intersections)
        rows.append({
            "agent": w.target_label,
            "role": w.role,
            "duration_seconds": w.duration_seconds,
            "overlap_seconds": overlap,
            "overlap_fraction": overlap / w.duration_seconds if w.duration_seconds else float("nan"),
            "overlaps_with": sorted(others),
        })
    rows.sort(key=lambda r: (r["overlap_seconds"], r["duration_seconds"]), reverse=True)
    return rows


def pairwise_overlap_rows(windows: Sequence[ActiveWindow]) -> List[dict]:
    rows = []
    for i, a in enumerate(windows):
        for b in windows[i + 1:]:
            start = max(a.start, b.start)
            end = min(a.end, b.end)
            if end <= start:
                continue
            rows.append({
                "agent_a": a.target_label,
                "role_a": a.role,
                "agent_b": b.target_label,
                "role_b": b.role,
                "start": start,
                "end": end,
                "overlap_seconds": (end - start).total_seconds(),
            })
    rows.sort(key=lambda r: r["overlap_seconds"], reverse=True)
    return rows


def parse_successor_map(specs: Sequence[str], stage_roles: set[str], profile: str = "generic") -> Dict[str, str]:
    mapping = {
        src: dst for src, dst in DEFAULT_SUCCESSOR_MAP.items()
        if profile == "staged" and src in stage_roles and dst in stage_roles
    }
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"invalid --successor {spec!r}; expected ROLE=ROLE")
        src, dst = (part.strip().lower() for part in spec.split("=", 1))
        if not src or not dst:
            raise ValueError(f"invalid --successor {spec!r}; expected ROLE=ROLE")
        mapping[src] = dst
    return mapping


def overlap_classification(old: ActiveWindow, new: ActiveWindow,
                           successor_map: Dict[str, str]) -> Optional[Tuple[str, str]]:
    same_role = new.role == old.role and old.role != "unknown"
    successor = successor_map.get(old.role)
    successor_role = bool(successor and new.role == successor)
    if not same_role and not successor_role:
        return None
    reason = "later-same-role-spawn" if same_role else f"successor-{new.role}-spawn"
    if old.chunk_key and new.chunk_key:
        if old.chunk_key != new.chunk_key:
            return reason, "cross-chunk-overlap"
        if same_role:
            return reason, "same-chunk-replacement-overlap"
        return reason, "same-chunk-repair-overlap"
    return reason, "unclassified-overlap"


def build_lingering_exposures(family: finder.Family,
                               parsed: Dict[str, lifecycle.ParsedSession],
                               windows: Sequence[ActiveWindow],
                               prices: Dict[str, Tuple[float, float, float]],
                               successor_map: Dict[str, str]) -> List[LingeringExposure]:
    """Classify observed activity after overlapping same/successor-role spawns.

    v4 distinguishes explicit same-chunk repair/replacement from cross-chunk
    overlap when strong structured assignment labels are available. Without a
    strong chunk label, overlap stays unclassified rather than being called waste.
    One earliest trigger is retained per classification for each older worker.
    """
    ordered = sorted(windows, key=lambda w: (w.spawn_ts, w.start, w.index))
    rows: List[LingeringExposure] = []
    for w in ordered:
        grouped: Dict[str, List[Tuple[datetime, str, ActiveWindow]]] = defaultdict(list)
        for other in ordered:
            if other.target_key == w.target_key or other.spawn_ts <= w.spawn_ts:
                continue
            if other.spawn_ts >= w.end:
                continue
            classified = overlap_classification(w, other, successor_map)
            if classified is None:
                continue
            reason, classification = classified
            grouped[classification].append((other.spawn_ts, reason, other))
        for classification, candidates in grouped.items():
            trigger_ts, reason, trigger = min(candidates, key=lambda x: (x[0], x[2].index))
            child = aggregate_requests(request_slice(parsed, [w.target_key], trigger_ts, w.end), prices)
            root = aggregate_requests(request_slice(parsed, [family.root], trigger_ts, w.end), prices)
            rows.append(LingeringExposure(
                window=w,
                trigger_window=trigger,
                trigger_ts=trigger_ts,
                reason=reason,
                classification=classification,
                child_after_trigger=child,
                root_during_lingering=root,
            ))
    priority = {
        "cross-chunk-overlap": 3,
        "unclassified-overlap": 2,
        "same-chunk-replacement-overlap": 1,
        "same-chunk-repair-overlap": 0,
    }
    rows.sort(key=lambda r: (priority.get(r.classification, -1), r.child_after_trigger.api_eq, r.lingering_seconds), reverse=True)
    return rows


def role_costs(family: finder.Family,
               sessions: Dict[str, finder.Session],
               parsed: Dict[str, lifecycle.ParsedSession],
               prices: Dict[str, Tuple[float, float, float]]) -> Dict[str, TokenTotals]:
    out: Dict[str, TokenTotals] = defaultdict(TokenTotals)
    for key in family.members:
        role = lifecycle.role_for_key(key, family, sessions)
        for req in parsed[key].usage:
            out[role].add_request(req, prices)
    return out


def build_bursts_for_session(key: str, label: str, role: str,
                             usage: Sequence[lifecycle.UsageRequest],
                             gap_seconds: float,
                             prices: Dict[str, Tuple[float, float, float]]) -> List[Burst]:
    if not usage:
        return []
    reqs = sorted(usage, key=lambda r: r.ts)
    groups: List[List[lifecycle.UsageRequest]] = [[reqs[0]]]
    for req in reqs[1:]:
        if (req.ts - groups[-1][-1].ts).total_seconds() > gap_seconds:
            groups.append([req])
        else:
            groups[-1].append(req)
    out = []
    for i, group in enumerate(groups, 1):
        t = aggregate_requests(group, prices)
        out.append(Burst(
            session_key=key,
            agent_label=label,
            role=role,
            index=i,
            start=group[0].ts,
            end=group[-1].ts,
            totals=t,
            first_input=group[0].input_tokens,
            last_input=group[-1].input_tokens,
            peak_input=max(r.input_tokens for r in group),
        ))
    return out


def build_all_bursts(family: finder.Family,
                     sessions: Dict[str, finder.Session],
                     parsed: Dict[str, lifecycle.ParsedSession],
                     labels: Dict[str, str],
                     gap_seconds: float,
                     prices: Dict[str, Tuple[float, float, float]]) -> Dict[str, List[Burst]]:
    out = {}
    for key in family.members:
        role = lifecycle.role_for_key(key, family, sessions)
        out[key] = build_bursts_for_session(key, labels[key], role, parsed[key].usage, gap_seconds, prices)
    return out


def request_slice(parsed: Dict[str, lifecycle.ParsedSession], keys: Iterable[str],
                  start: datetime, end: datetime) -> List[lifecycle.UsageRequest]:
    rows = []
    for key in keys:
        rows.extend(r for r in parsed[key].usage if start <= r.ts < end)
    return rows


def build_cycles(family: finder.Family,
                 sessions: Dict[str, finder.Session],
                 parsed: Dict[str, lifecycle.ParsedSession],
                 windows: Sequence[ActiveWindow],
                 prices: Dict[str, Tuple[float, float, float]],
                 handoff_seconds: float,
                 cycle_roles: Optional[Tuple[str, str]] = None) -> List[Cycle]:
    """Build explicitly configured role-pair cycles from trusted spawn order.

    Root supervision windows follow the observed lifetime of each child session,
    not the next spawn. Overlapping cycles remain visible but are excluded from
    clean supervision-ratio summaries.
    """
    if cycle_roles is None:
        return []
    cycles: List[Cycle] = []
    guardians = [k for k in family.members if lifecycle.role_for_key(k, family, sessions) == "guardian/auto-review"]
    all_keys = list(family.members)
    ordered = sorted(windows, key=lambda w: (w.spawn_ts, w.start, w.index))

    idx = 0
    for i in range(len(ordered) - 1):
        imp = ordered[i]
        val = ordered[i + 1]
        if (imp.role, val.role) != cycle_roles:
            continue
        idx += 1
        imp_end, val_end = imp.end, val.end
        handoff = (val.start - imp_end).total_seconds()
        truncated = any(w.start > w.spawn_ts or
                        (sessions[w.target_key].last_ts is not None and
                         w.end < sessions[w.target_key].last_ts)
                        for w in (imp, val))
        if truncated:
            quality = "window-truncated"
        elif imp.chunk_key and val.chunk_key and imp.chunk_key != val.chunk_key:
            quality = "chunk-mismatch"
        elif -2 <= handoff <= 2:
            quality = "tight-sequential"
        elif 0 < handoff <= handoff_seconds:
            quality = "sequential"
        elif handoff < -2:
            quality = "overlap"
        else:
            quality = "long-gap"

        root_imp = aggregate_requests(request_slice(parsed, [family.root], imp.start, imp_end), prices)
        root_val = aggregate_requests(request_slice(parsed, [family.root], val.start, val_end), prices)
        imp_agent = aggregate_requests(request_slice(parsed, [imp.target_key], imp.start, imp_end), prices)
        val_agent = aggregate_requests(request_slice(parsed, [val.target_key], val.start, val_end), prices)
        start, end = imp.start, max(val_end, val.start)
        guardian = aggregate_requests(request_slice(parsed, guardians, start, end), prices)
        all_work = aggregate_requests(request_slice(parsed, all_keys, start, end), prices)
        other_roles = []
        other_agents = []
        for other in windows:
            if other.target_key in {imp.target_key, val.target_key}:
                continue
            if other.start < end and other.end > start:
                other_roles.append(other.role)
                other_agents.append(f"{other.target_label}({other.role})")
        cycles.append(Cycle(
            index=idx,
            implementer_window=imp,
            validator_window=val,
            handoff_gap_seconds=handoff,
            quality=quality,
            root_implementer=root_imp,
            root_validator=root_val,
            implementer_agent=imp_agent,
            validator_agent=val_agent,
            guardian=guardian,
            all_work=all_work,
            other_active_roles=tuple(sorted(set(other_roles))),
            other_active_agents=tuple(sorted(set(other_agents))),
        ))
    return cycles

def large_small_breakdown(family: finder.Family,
                          sessions: Dict[str, finder.Session],
                          parsed: Dict[str, lifecycle.ParsedSession],
                          windows: Sequence[ActiveWindow],
                          prices: Dict[str, Tuple[float, float, float]],
                          input_threshold: int,
                          output_threshold: int) -> Tuple[Dict[str, TokenTotals], Dict[str, TokenTotals]]:
    by_role: Dict[str, TokenTotals] = defaultdict(TokenTotals)
    root_by_stage: Dict[str, TokenTotals] = defaultdict(TokenTotals)
    for key in family.members:
        role = lifecycle.role_for_key(key, family, sessions)
        for req in parsed[key].usage:
            if req.input_tokens < input_threshold or req.output_tokens > output_threshold:
                continue
            by_role[role].add_request(req, prices)
            if key == family.root:
                state = active_state_label(req.ts, windows)
                root_by_stage[state].add_request(req, prices)
    return by_role, root_by_stage


def session_context_stats(family: finder.Family,
                          sessions: Dict[str, finder.Session],
                          parsed: Dict[str, lifecycle.ParsedSession],
                          labels: Dict[str, str],
                          prices: Dict[str, Tuple[float, float, float]],
                          input_threshold: int,
                          output_threshold: int) -> List[dict]:
    rows = []
    for key in family.members:
        reqs = parsed[key].usage
        if not reqs:
            continue
        ins = [r.input_tokens for r in reqs]
        large = [r for r in reqs if r.input_tokens >= input_threshold and r.output_tokens <= output_threshold]
        resets = 0
        for a, b in zip(ins, ins[1:]):
            if a >= input_threshold and b < 0.55 * a:
                resets += 1
        totals = aggregate_requests(reqs, prices)
        large_totals = aggregate_requests(large, prices)
        rows.append({
            "key": key,
            "label": labels[key],
            "role": lifecycle.role_for_key(key, family, sessions),
            "requests": len(reqs),
            "first_input": ins[0],
            "last_input": ins[-1],
            "median_input": int(statistics.median(ins)),
            "p90_input": int(percentile(ins, 0.90)),
            "peak_input": max(ins),
            "context_resets": resets,
            "totals": totals,
            "large_small": large_totals,
        })
    rows.sort(key=lambda r: (r["large_small"].api_eq, r["large_small"].total_tokens, r["totals"].total_tokens), reverse=True)
    return rows


def value_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return type(value).__name__


def scalar_hash(value: object) -> Optional[str]:
    if not lifecycle.compact_scalar(value):
        return None
    return lifecycle.hash_id(value)


def is_id_shaped(value: object) -> bool:
    return lifecycle.looks_like_identifier(value)


def schema_walk(obj: object, roles: Sequence[str], path: Tuple[str, ...] = (),
                depth: int = 0) -> Iterable[Tuple[str, str, Optional[str], bool, bool]]:
    """Yield (path, type, scalar_hash, id_shaped, role_label_like).

    No values are returned. Noisy/content-bearing fields are skipped entirely.
    """
    if depth > 9:
        return
    obj = lifecycle.parse_jsonish(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).lower()
            if k in lifecycle.NOISY_KEYS:
                continue
            p2 = path + (k,)
            ptxt = ".".join(p2)
            typ = value_type(value)
            sh = scalar_hash(value)
            role_like = False
            if isinstance(value, str):
                low = value.lower()
                role_like = any(role in low for role in roles)
            yield ptxt, typ, sh, is_id_shaped(value), role_like
            if isinstance(value, (dict, list)):
                yield from schema_walk(value, roles, p2, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:256]:
            p2 = path + ("[]",)
            ptxt = ".".join(p2)
            typ = value_type(value)
            sh = scalar_hash(value)
            role_like = isinstance(value, str) and any(role in value.lower() for role in roles)
            yield ptxt, typ, sh, is_id_shaped(value), role_like
            if isinstance(value, (dict, list)):
                yield from schema_walk(value, roles, p2, depth + 1)
    else:
        ptxt = ".".join(path) or "<root>"
        yield ptxt, value_type(obj), scalar_hash(obj), is_id_shaped(obj), False


def action_name_and_args(node: dict) -> Tuple[Optional[str], Optional[str], object, object]:
    names = []
    if isinstance(node.get("name"), str):
        names.append(node["name"])
    if isinstance(node.get("tool_name"), str):
        names.append(node["tool_name"])
    fn = node.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        names.append(fn["name"])
    name = next((n for n in names if lifecycle.classify_action(n)), None)
    kind = lifecycle.classify_action(name) if name else None
    args = node.get("arguments")
    if args is None:
        args = node.get("args")
    if args is None and isinstance(fn, dict):
        args = fn.get("arguments")
    call_raw = node.get("call_id") or node.get("tool_call_id") or node.get("function_call_id") or node.get("id")
    return name, kind, lifecycle.parse_jsonish(args), call_raw


def output_record_raw(payload: object) -> Optional[Tuple[str, object]]:
    if not isinstance(payload, dict):
        return None
    ptype = str(payload.get("type", "")).lower()
    call_raw = payload.get("call_id") or payload.get("tool_call_id") or payload.get("function_call_id")
    if call_raw is None:
        return None
    if not ("output" in ptype or "result" in ptype or "output" in payload or "result" in payload):
        return None
    call_id = lifecycle.hash_id(call_raw)
    if not call_id:
        return None
    return call_id, lifecycle.parse_jsonish(payload.get("output", payload.get("result")))


def schema_stat(audit_obj: SchemaAudit, key: Tuple[str, str, str]) -> SchemaStat:
    if key not in audit_obj.stats:
        audit_obj.stats[key] = SchemaStat()
    return audit_obj.stats[key]


def build_schema_audit(family: finder.Family,
                       sessions: Dict[str, finder.Session],
                       parsed: Dict[str, lifecycle.ParsedSession],
                       roles: Sequence[str],
                       analysis_start: Optional[datetime] = None,
                       analysis_end: Optional[datetime] = None) -> SchemaAudit:
    out = SchemaAudit()
    family_ids = set()
    for key in family.members:
        family_ids |= sessions[key].links.own_ids

    call_to_action: Dict[str, lifecycle.ActionEvent] = {}
    for key in family.members:
        for action in parsed[key].actions:
            if action.call_id:
                call_to_action[action.call_id] = action

    # First pass: invocation argument schema and compact scalar namespaces.
    for key in family.members:
        path = sessions[key].path
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            local_index = 0
            for raw in fh:
                low = raw.lower()
                if not any(x in low for x in (b'spawn_agent', b'send_input', b'wait', b'resume_agent', b'close_agent', b'create_agent')):
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ts = lifecycle.parse_ts(obj.get("timestamp"))
                if ts is None or not in_time_window(ts, analysis_start, analysis_end):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                for node in lifecycle.action_node_candidates(payload):
                    name, kind, args, call_raw = action_name_and_args(node)
                    if not kind:
                        continue
                    call_id = lifecycle.hash_id(call_raw) if call_raw is not None else None
                    local_index += 1
                    cid = call_id or f"local:{key}:{local_index}"
                    out.call_totals[(kind, "args")] += 1
                    seen_paths = set()
                    for ptxt, typ, sh, idshape, role_like in schema_walk(args, roles):
                        st = schema_stat(out, (kind, "args", ptxt))
                        st.occurrences += 1
                        st.types[typ] += 1
                        if ptxt not in seen_paths:
                            st.calls_present.add(cid)
                            seen_paths.add(ptxt)
                        if sh:
                            st.scalar_hashes.add(sh)
                            if call_id:
                                out.action_arg_tokens[call_id].add(sh)
                            if sh in family_ids:
                                st.family_id_matches += 1
                        if idshape:
                            st.id_shaped += 1
                        if role_like:
                            st.role_label_hits += 1

    # Second pass: output/result schema. Also build a privacy-safe namespace of
    # compact result tokens from trusted spawns so we can test whether later
    # action arguments reuse those handles.
    for key in family.members:
        path = sessions[key].path
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                if b'"call_id"' not in raw and b'"tool_call_id"' not in raw and b'"function_call_id"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ts = lifecycle.parse_ts(obj.get("timestamp"))
                if ts is None or not in_time_window(ts, analysis_start, analysis_end):
                    continue
                payload = obj.get("payload")
                rec = output_record_raw(payload)
                if not rec:
                    continue
                call_id, result = rec
                action = call_to_action.get(call_id)
                if action is None:
                    continue
                kind = action.kind
                out.call_totals[(kind, "result")] += 1
                seen_paths = set()
                result_tokens = set()
                for ptxt, typ, sh, idshape, role_like in schema_walk(result, roles):
                    st = schema_stat(out, (kind, "result", ptxt))
                    st.occurrences += 1
                    st.types[typ] += 1
                    if ptxt not in seen_paths:
                        st.calls_present.add(call_id)
                        seen_paths.add(ptxt)
                    if sh:
                        st.scalar_hashes.add(sh)
                        result_tokens.add(sh)
                        if sh in family_ids:
                            st.family_id_matches += 1
                    if idshape:
                        st.id_shaped += 1
                    if role_like:
                        st.role_label_hits += 1

                if action.kind == "spawn" and lifecycle.is_trusted_action(action) and action.matched_session:
                    for token in result_tokens:
                        out.spawn_output_token_to_children[token].add(action.matched_session)

    # Cross-namespace bridge audit. This is diagnostic only: if an unresolved
    # send/wait argument reuses a unique token from a trusted spawn result, that
    # is a promising exact handle bridge for a later extractor revision.
    for key in family.members:
        for action in parsed[key].actions:
            if action.kind not in {"send", "wait", "resume", "close"} or lifecycle.is_trusted_action(action):
                continue
            if not action.call_id:
                continue
            candidates = set()
            for token in out.action_arg_tokens.get(action.call_id, set()):
                children = out.spawn_output_token_to_children.get(token, set())
                if len(children) == 1:
                    candidates |= children
            if len(candidates) == 1:
                out.bridge_candidates[f"{action.kind}_unique_spawn_handle"] += 1
            elif len(candidates) > 1:
                out.bridge_candidates[f"{action.kind}_ambiguous_spawn_handle"] += 1

    # Annotate schema paths whose values overlap trusted spawn-result handles.
    spawn_tokens = set(out.spawn_output_token_to_children)
    for (kind, side, ptxt), st in out.stats.items():
        if side == "args" and kind in {"send", "wait", "resume", "close"}:
            st.spawn_handle_overlap = len(st.scalar_hashes & spawn_tokens)
    return out


def exact_role_target_from_args(args: object, roles: Sequence[str]) -> Optional[str]:
    """Return a role only when a routing field exactly names one configured role.

    This is intentionally stricter than generic role-hint extraction. A value such
    as "validator" under target/recipient is trusted at the role level. A longer
    prose string merely mentioning validator is not.
    """
    role_set = {r.strip().lower() for r in roles if r.strip()}
    found: set[str] = set()

    def walk(obj: object, depth: int = 0) -> None:
        if depth > 8:
            return
        obj = lifecycle.parse_jsonish(obj)
        if isinstance(obj, dict):
            for key, value in obj.items():
                k = str(key).lower()
                if k in lifecycle.NOISY_KEYS:
                    continue
                if k in {"target", "recipient", "target_role", "recipient_role"} and isinstance(value, str):
                    low = value.strip().lower()
                    if low in role_set:
                        found.add(low)
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj[:128]:
                walk(value, depth + 1)

    walk(args)
    return next(iter(found)) if len(found) == 1 else None


def build_exact_role_target_map(family: finder.Family,
                                sessions: Dict[str, finder.Session],
                                roles: Sequence[str],
                                analysis_start: Optional[datetime] = None,
                                analysis_end: Optional[datetime] = None) -> Dict[str, str]:
    """Map hashed action call IDs to exact role-level targets from safe routing fields."""
    out: Dict[str, str] = {}
    for key in family.members:
        path = sessions[key].path
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                low = raw.lower()
                if b'send_input' not in low and b'send_message' not in low and b'message_agent' not in low:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ts = lifecycle.parse_ts(obj.get("timestamp"))
                if ts is None or not in_time_window(ts, analysis_start, analysis_end):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                for node in lifecycle.action_node_candidates(payload):
                    _, kind, args, call_raw = action_name_and_args(node)
                    if kind != "send" or call_raw is None:
                        continue
                    role = exact_role_target_from_args(args, roles)
                    if role is None:
                        continue
                    call_id = lifecycle.hash_id(call_raw)
                    if not call_id:
                        continue
                    # Conflicting exact role labels are treated as unresolved.
                    if call_id in out and out[call_id] != role:
                        out[call_id] = "__ambiguous__"
                    else:
                        out[call_id] = role
    return {k: v for k, v in out.items() if v != "__ambiguous__"}


def role_targeted_root_send_costs(family: finder.Family,
                                  parsed: Dict[str, lifecycle.ParsedSession],
                                  role_target_map: Dict[str, str],
                                  prices: Dict[str, Tuple[float, float, float]],
                                  window_seconds: float) -> Tuple[Counter, Dict[str, TokenTotals], int]:
    """Count exact role-targeted root SENDs and pair nearby root inference heuristically.

    The SEND role itself is trusted because it comes from an exact configured role
    label in a routing field. The nearby inference pairing remains observational.
    """
    counts: Counter = Counter()
    totals: Dict[str, TokenTotals] = defaultdict(TokenTotals)
    actions = [
        a for a in parsed[family.root].actions
        if a.kind == "send" and a.call_id and a.call_id in role_target_map
    ]
    actions.sort(key=lambda a: a.ts)
    usage = parsed[family.root].usage
    used_req: set[int] = set()
    unmatched = 0
    for action in actions:
        role = role_target_map[action.call_id]
        counts[role] += 1
        candidates = []
        for i, req in enumerate(usage):
            if i in used_req:
                continue
            delta = (action.ts - req.ts).total_seconds()
            if -15 <= delta <= window_seconds:
                penalty = abs(delta) + (0 if delta >= 0 else 20)
                candidates.append((penalty, abs(delta), i, req))
        if not candidates:
            unmatched += 1
            continue
        candidates.sort(key=lambda x: (x[0], x[1]))
        _, _, i, req = candidates[0]
        used_req.add(i)
        totals[role].add_request(req, prices)
    return counts, totals, unmatched


def print_token_row(name: str, t: TokenTotals, denom_tokens: int,
                    denom_eq: float, width: int = 32) -> None:
    raw_share = t.total_tokens / denom_tokens if denom_tokens else float("nan")
    eq_share = t.api_eq / denom_eq if denom_eq else float("nan")
    print(
        f"{name:<{width}} {t.requests:>6} {fmt_tokens(t.input_tokens):>10} "
        f"{fmt_pct(t.cache_share):>7} {fmt_tokens(t.uncached_input_tokens):>10} "
        f"{fmt_tokens(t.output_tokens):>9} {fmt_eq(t.api_eq):>10} "
        f"{fmt_pct(raw_share):>7} {fmt_pct(eq_share):>7}"
    )


def print_schema_audit(schema: SchemaAudit, limit: int) -> None:
    print("\nAction schema audit")
    print("-------------------")
    print("Only field paths/types/counts are shown. Values are never printed.")
    if schema.bridge_candidates:
        print("candidate spawn-result handle bridges: " + ", ".join(
            f"{k}={v}" for k, v in sorted(schema.bridge_candidates.items())
        ))
    else:
        print("candidate spawn-result handle bridges: none detected")

    rows = []
    for (kind, side, path), st in schema.stats.items():
        priority = (
            1 if st.spawn_handle_overlap else 0,
            1 if st.family_id_matches else 0,
            st.role_label_hits,
            len(st.calls_present),
            st.occurrences,
        )
        rows.append((priority, kind, side, path, st))
    rows.sort(key=lambda x: x[0], reverse=True)
    if not rows:
        print("No structural action argument/result schema was found.")
        return

    print("kind   side    path                                      calls   types          unique  idlike  family-id  spawn-overlap  role-like")
    shown = 0
    for _, kind, side, path, st in rows:
        total_calls = schema.call_totals.get((kind, side), 0)
        types = ",".join(k for k, _ in st.types.most_common(2))
        print(
            f"{kind:<6} {side:<7} {path[:41]:<41} "
            f"{len(st.calls_present):>4}/{total_calls:<4} {types[:13]:<13} "
            f"{len(st.scalar_hashes):>6} {st.id_shaped:>7} {st.family_id_matches:>10} "
            f"{st.spawn_handle_overlap:>13} {st.role_label_hits:>10}"
        )
        shown += 1
        if shown >= limit:
            break


def _ratio(num: float, den: float) -> Optional[float]:
    return num / den if den else None


def _sequential_cycles(cycles: Sequence[Cycle]) -> List[Cycle]:
    return [c for c in cycles if c.quality in {"tight-sequential", "sequential"}]


def _isolated_sequential_cycles(cycles: Sequence[Cycle]) -> List[Cycle]:
    return [c for c in _sequential_cycles(cycles) if c.isolated]


def _cycle_supervision_summary(cycles: Sequence[Cycle]) -> dict:
    sequential = _sequential_cycles(cycles)
    isolated = _isolated_sequential_cycles(cycles)

    def summarize(rows: Sequence[Cycle]) -> dict:
        root_impl = sum(c.root_implementer.api_eq for c in rows)
        root_val = sum(c.root_validator.api_eq for c in rows)
        child_impl = sum(c.implementer_agent.api_eq for c in rows)
        child_val = sum(c.validator_agent.api_eq for c in rows)
        root_raw = sum(c.root_implementer.total_tokens + c.root_validator.total_tokens for c in rows)
        child_raw = sum(c.implementer_agent.total_tokens + c.validator_agent.total_tokens for c in rows)
        def ratio(a: float, b: float):
            return a / b if b else None
        return {
            "cycles": len(rows),
            "root_first_role_api_eq": root_impl,
            "first_agent_api_eq": child_impl,
            "root_second_role_api_eq": root_val,
            "second_agent_api_eq": child_val,
            "combined_root_api_eq": root_impl + root_val,
            "combined_child_api_eq": child_impl + child_val,
            "first_role_ratio_api_eq": ratio(root_impl, child_impl),
            "second_role_ratio_api_eq": ratio(root_val, child_val),
            "combined_supervision_ratio_api_eq": ratio(root_impl + root_val, child_impl + child_val),
            "combined_supervision_ratio_raw": ratio(root_raw, child_raw),
        }

    return {
        "sequential_cycles_observed": len(sequential),
        "ratios_suppressed_for_nonisolated_cycles": len(sequential) - len(isolated),
        "isolated_sequential": summarize(isolated),
        "note": "Overall supervision ratios are reported only for isolated sequential cycles.",
    }


def chunk_rows(family: finder.Family,
               parsed: Dict[str, lifecycle.ParsedSession],
               windows: Sequence[ActiveWindow],
               prices: Dict[str, Tuple[float, float, float]]) -> List[dict]:
    grouped: Dict[str, List[ActiveWindow]] = defaultdict(list)
    for w in windows:
        if w.chunk_label:
            grouped[w.chunk_label].append(w)
    rows = []
    for chunk_label, chunk_windows in grouped.items():
        child = TokenTotals()
        seen_targets = set()
        intervals = []
        roles = Counter()
        for w in chunk_windows:
            roles[w.role] += 1
            intervals.append((w.start, w.end))
            if w.target_key in seen_targets:
                continue
            seen_targets.add(w.target_key)
            child.add_totals(aggregate_requests(request_slice(parsed, [w.target_key], w.start, w.end), prices))
        root_reqs = []
        for req in parsed[family.root].usage:
            if any(a <= req.ts < b for a, b in intervals):
                root_reqs.append(req)
        root = aggregate_requests(root_reqs, prices)
        rows.append({
            "chunk": chunk_label,
            "start": min(w.start for w in chunk_windows),
            "end": max(w.end for w in chunk_windows),
            "assignments": len(chunk_windows),
            "roles": dict(sorted(roles.items())),
            "child": child,
            "root_during_chunk_windows": root,
        })
    rows.sort(key=lambda r: (r["start"], r["chunk"]))
    return rows


def exposure_totals_by_class(exposures: Sequence[LingeringExposure]) -> Dict[str, TokenTotals]:
    out: Dict[str, TokenTotals] = defaultdict(TokenTotals)
    # One exposure per old worker/classification; different classifications can
    # overlap by design, so the classes are descriptive and not additive.
    for r in exposures:
        if r.child_after_trigger.requests:
            out[r.classification].add_totals(r.child_after_trigger)
    return out


def comparison_metrics(family: finder.Family,
                       role_totals: Dict[str, TokenTotals],
                       root_child_counts: Dict[str, TokenTotals],
                       concurrency: dict,
                       context_rows: Sequence[dict],
                       large_by_role: Dict[str, TokenTotals],
                       exposures: Sequence[LingeringExposure],
                       windows: Sequence[ActiveWindow],
                       chunks: Sequence[dict],
                       compaction: Optional[CompactionAudit] = None) -> dict:
    total = TokenTotals()
    for t in role_totals.values():
        total.add_totals(t)
    root = next((v for k, v in role_totals.items() if k.endswith("/root")), TokenTotals())
    root_context = next((r for r in context_rows if r.get("label") == "ROOT"), None)
    large = TokenTotals()
    for t in large_by_role.values():
        large.add_totals(t)
    exp = exposure_totals_by_class(exposures)
    correlated = sum(1 for w in windows if w.chunk_label)
    child_chunk = TokenTotals()
    for r in chunks:
        child_chunk.add_totals(r["child"])
    concurrent_root = TokenTotals()
    for bucket in ("2", "3+"):
        if bucket in root_child_counts:
            concurrent_root.add_totals(root_child_counts[bucket])
    n_chunks = len(chunks)
    out = {
        "workflow_api_eq": total.api_eq,
        "workflow_raw_tokens": total.total_tokens,
        "root_api_eq_share": root.api_eq / total.api_eq if total.api_eq else None,
        "root_raw_share": root.total_tokens / total.total_tokens if total.total_tokens else None,
        "root_requests": root.requests,
        "root_median_input_tokens": root_context["median_input"] if root_context else None,
        "root_p90_input_tokens": root_context["p90_input"] if root_context else None,
        "large_context_small_output_api_eq_share": large.api_eq / total.api_eq if total.api_eq else None,
        "root_with_2plus_children_api_eq_share": concurrent_root.api_eq / root.api_eq if root.api_eq else None,
        "recognized_child_agent_hours": concurrency.get("agent_seconds", 0.0) / 3600.0,
        "extra_concurrency_hours": concurrency.get("extra_concurrency_seconds", 0.0) / 3600.0,
        "peak_concurrent_children": concurrency.get("peak_concurrent_children", 0),
        "trusted_active_windows": len(windows),
        "chunk_correlated_windows": correlated,
        "chunk_correlation_coverage": correlated / len(windows) if windows else None,
        "correlated_chunks": n_chunks,
        "direct_child_requests_per_correlated_chunk": child_chunk.requests / n_chunks if n_chunks else None,
        "direct_child_api_eq_per_correlated_chunk": child_chunk.api_eq / n_chunks if n_chunks else None,
        "cross_chunk_overlap_api_eq": exp.get("cross-chunk-overlap", TokenTotals()).api_eq,
        "same_chunk_repair_overlap_api_eq": exp.get("same-chunk-repair-overlap", TokenTotals()).api_eq,
        "same_chunk_replacement_overlap_api_eq": exp.get("same-chunk-replacement-overlap", TokenTotals()).api_eq,
        "unclassified_overlap_api_eq": exp.get("unclassified-overlap", TokenTotals()).api_eq,
    }
    if compaction is not None:
        direct_coverage = (
            compaction.direct_matched / compaction.explicit_count
            if compaction.explicit_count else None
        )
        recovery_share = (
            compaction.recovery_totals.api_eq / total.api_eq if total.api_eq else None
        )
        root_role = next((v for k, v in compaction.by_role.items() if k.endswith("/root")), {})
        out.update({
            "explicit_compactions": compaction.explicit_count,
            "direct_compaction_usage_coverage": direct_coverage,
            "direct_compaction_api_eq": compaction.direct_totals.api_eq,
            "direct_compaction_api_eq_outside_primary_totals": compaction.direct_outside_primary_totals.api_eq,
            "post_compaction_recovery_api_eq": compaction.recovery_totals.api_eq,
            "post_compaction_recovery_api_eq_share": recovery_share,
            "post_compaction_recovery_input_tokens": compaction.recovery_totals.input_tokens,
            "post_compaction_recovery_requests": compaction.recovery_totals.requests,
            "root_compactions": root_role.get("compactions", 0),
            "root_compactions_per_hour": root_role.get("compactions_per_hour"),
            "repeated_post_compaction_read_events": sum(e.repeated_read_events for e in compaction.events),
            "recovery_api_eq_delta_vs_same_session_baseline": compaction.baseline.get(
                "api_eq_delta_vs_same_session_baseline"
            ),
        })
    return out


def export_json(path: str, family: finder.Family,
                sessions: Dict[str, finder.Session],
                parsed: Dict[str, lifecycle.ParsedSession],
                labels: Dict[str, str],
                windows: Sequence[ActiveWindow],
                roles: Dict[str, TokenTotals],
                root_states: Dict[str, TokenTotals],
                bursts: Dict[str, List[Burst]],
                cycles: Sequence[Cycle],
                context_rows: Sequence[dict],
                large_by_role: Dict[str, TokenTotals],
                large_root_state: Dict[str, TokenTotals],
                root_child_counts: Dict[str, TokenTotals],
                concurrency: dict,
                overlap_agents: Sequence[dict],
                overlap_pairs: Sequence[dict],
                lingering: Sequence[LingeringExposure],
                successor_map: Dict[str, str],
                role_target_counts: Counter,
                role_target_costs: Dict[str, TokenTotals],
                role_target_unmatched: int,
                schema: Optional[SchemaAudit],
                analysis_meta: AnalysisMeta,
                chunks: Sequence[dict],
                comparison: dict,
                compaction: Optional[CompactionAudit],
                nested: Optional[dict] = None,
                workflow_analysis: Optional[dict] = None,
                pause_analysis: Optional[dict] = None) -> None:
    def tt(t: TokenTotals) -> dict:
        return {
            "requests": t.requests,
            "input_tokens": t.input_tokens,
            "cached_input_tokens": t.cached_input_tokens,
            "uncached_input_tokens": t.uncached_input_tokens,
            "output_tokens": t.output_tokens,
            "reasoning_output_tokens": t.reasoning_tokens,
            "api_list_equivalent_usd": round(t.api_eq, 6),
            "price_coverage": t.price_coverage if math.isfinite(t.price_coverage) else None,
        }

    supervision = _cycle_supervision_summary(cycles)
    obj = {
        "schema": "codex-workflow-cost-profile-v6.1",
        "version": __version__,
        "family": family.family_key,
        "privacy": "No prompts/responses/source code/tool output/raw IDs are included.",
        "api_eq_note": "Public API-list-price equivalent only; not subscription billing or OpenAI internal cost.",
        "root_role": lifecycle.role_for_session(sessions[family.root]),
        "workflow_analysis": workflow_analysis,
        "pause_analysis": pause_analysis,
        "nested_attribution": nested,
        "tool_activity": compaction.tool_activity if compaction is not None else None,
        "tool_activity_note": "Structural observed calls only. Opaque wrappers are not decoded by reading code strings. Counts are not waste or token-cost attribution; null means scanning was disabled.",
        "source_family_span": {
            "start": analysis_meta.source_family_start.isoformat() if analysis_meta.source_family_start else None,
            "end": analysis_meta.source_family_end.isoformat() if analysis_meta.source_family_end else None,
        },
        "analysis_window": {
            "after": analysis_meta.requested_after.isoformat() if analysis_meta.requested_after else None,
            "before": analysis_meta.requested_before.isoformat() if analysis_meta.requested_before else None,
            "analysis_start": analysis_meta.analysis_start.isoformat() if analysis_meta.analysis_start else None,
            "analysis_end": analysis_meta.analysis_end.isoformat() if analysis_meta.analysis_end else None,
            "original_family_root": analysis_meta.original_family_root,
            "analysis_root": analysis_meta.analysis_root,
            "root_selection_method": analysis_meta.root_selection_method,
            "root_selection_confidence": analysis_meta.root_selection_confidence,
            "membership_method": analysis_meta.membership_method,
            "primary_sessions": len(family.members),
            "pre_window_sessions": len(analysis_meta.pre_window),
            "carry_in_sessions": len(analysis_meta.carry_in),
            "in_window_sessions": len(analysis_meta.in_window),
            "post_window_sessions": len(analysis_meta.post_window),
            "carry_out_sessions": len(analysis_meta.carry_out),
            "excluded_in_window_sessions": len(analysis_meta.excluded_in_window),
            "carry_in_activity_in_window": tt(analysis_meta.carry_in_totals),
            "root_candidates": [
                {
                    **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
                } for row in analysis_meta.orchestrator_candidates[:10]
            ],
        },
        "role_costs": {k: tt(v) for k, v in sorted(roles.items())},
        "root_active_state_costs": {k: tt(v) for k, v in sorted(root_states.items())},
        "root_cost_by_concurrent_descendant_count": {k: tt(v) for k, v in sorted(root_child_counts.items())},
        "concurrency": {
            "recognized_child_agent_minutes": concurrency["agent_seconds"] / 60.0,
            "recognized_child_agent_hours": concurrency["agent_seconds"] / 3600.0,
            "recognized_child_active_wall_minutes": concurrency["active_wall_seconds"] / 60.0,
            "overlap_wall_minutes": concurrency["overlap_wall_seconds"] / 60.0,
            "extra_concurrency_hours": concurrency["extra_concurrency_seconds"] / 3600.0,
            "peak_concurrent_children": concurrency["peak_concurrent_children"],
            "wall_minutes_by_count": {k: v / 60.0 for k, v in concurrency["wall_seconds_by_count"].items()},
        },
        "successor_map_for_lingering_candidates": dict(successor_map),
        "lingering_candidates": [
            {
                "agent": r.window.target_label,
                "role": r.window.role,
                "trigger_agent": r.trigger_window.target_label,
                "trigger_role": r.trigger_window.role,
                "trigger": r.trigger_ts.isoformat(),
                "reason": r.reason,
                "classification": r.classification,
                "agent_chunk": r.window.chunk_label,
                "trigger_chunk": r.trigger_window.chunk_label,
                "lingering_seconds": r.lingering_seconds,
                "child_activity_after_trigger": tt(r.child_after_trigger),
                "root_activity_during_lingering_window": tt(r.root_during_lingering),
                "active_inference_after_trigger": r.active_after_trigger,
            } for r in lingering
        ],
        "agent_overlap": [
            {
                **{k: v for k, v in r.items() if k not in {"overlap_fraction"}},
                "overlap_fraction": r["overlap_fraction"] if math.isfinite(r["overlap_fraction"]) else None,
            } for r in overlap_agents
        ],
        "pairwise_overlap": [
            {
                **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}
            } for r in overlap_pairs
        ],
        "active_windows": [
            {
                "index": w.index,
                "role": w.role,
                "target": w.target_label,
                "spawn": w.spawn_ts.isoformat(),
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "duration_seconds": w.duration_seconds,
                "spawn_method": w.spawn_method,
                "spawn_confidence": w.spawn_confidence,
                "chunk": w.chunk_label,
                "assignment_source": w.assignment_source,
                "assignment_confidence": w.assignment_confidence,
            } for w in windows
        ],
        "chunk_correlation": {
            "trusted_active_windows": len(windows),
            "correlated_windows": sum(1 for w in windows if w.chunk_label),
            "correlated_chunks": len(chunks),
            "coverage": (sum(1 for w in windows if w.chunk_label) / len(windows)) if windows else None,
            "method": "explicit structured spawn task labels or validated assignment map; weak timing is never used",
            "chunks": [
                {
                    "chunk": r["chunk"],
                    "start": r["start"].isoformat(),
                    "end": r["end"].isoformat(),
                    "assignments": r["assignments"],
                    "roles": r["roles"],
                    "direct_child_work": tt(r["child"]),
                    "root_during_chunk_windows": tt(r["root_during_chunk_windows"]),
                } for r in chunks
            ],
        },
        "comparison_metrics": comparison,
        "exact_role_targeted_root_sends": {
            "counts": dict(role_target_counts),
            "associated_inference": {k: tt(v) for k, v in sorted(role_target_costs.items())},
            "unmatched_inference_pairings": role_target_unmatched,
            "note": "Role target is exact/trusted; nearby inference pairing is observational, not causal.",
        },
        "cycle_supervision_summary": supervision if cycles else None,
        "bursts": [
            {
                "agent": b.agent_label,
                "role": b.role,
                "index": b.index,
                "start": b.start.isoformat(),
                "end": b.end.isoformat(),
                "duration_seconds": b.duration_seconds,
                "first_input_tokens": b.first_input,
                "last_input_tokens": b.last_input,
                "peak_input_tokens": b.peak_input,
                "cost": tt(b.totals),
            }
            for key in sorted(bursts) for b in bursts[key]
        ],
        "cycles": [
            {
                "index": c.index,
                "quality": c.quality,
                "first_agent": c.implementer_window.target_label,
                "second_agent": c.validator_window.target_label,
                "first_role": c.implementer_window.role,
                "second_role": c.validator_window.role,
                "start": c.implementer_window.start.isoformat(),
                "end": c.validator_window.end.isoformat(),
                "handoff_gap_seconds": c.handoff_gap_seconds,
                "root_during_first_role": tt(c.root_implementer),
                "root_during_second_role": tt(c.root_validator),
                "first_agent_inference": tt(c.implementer_agent),
                "second_agent_inference": tt(c.validator_agent),
                "ratio_eligible": c.ratio_eligible,
                "supervision_ratio_api_eq": c.supervision_ratio_api_eq if c.ratio_eligible and math.isfinite(c.supervision_ratio_api_eq) else None,
                "first_role_supervision_ratio_api_eq": c.implementation_supervision_ratio_api_eq if c.ratio_eligible and math.isfinite(c.implementation_supervision_ratio_api_eq) else None,
                "second_role_supervision_ratio_api_eq": c.validation_supervision_ratio_api_eq if c.ratio_eligible and math.isfinite(c.validation_supervision_ratio_api_eq) else None,
                "isolated_from_other_observed_descendants": c.isolated,
                "other_active_roles": list(c.other_active_roles),
                "other_active_agents": list(c.other_active_agents),
                "guardian": tt(c.guardian),
                "all_observed_work": tt(c.all_work),
            } for c in cycles
        ],
        "context_sessions": [
            {
                "agent": r["label"],
                "role": r["role"],
                "requests": r["requests"],
                "first_input_tokens": r["first_input"],
                "median_input_tokens": r["median_input"],
                "p90_input_tokens": r["p90_input"],
                "peak_input_tokens": r["peak_input"],
                "last_input_tokens": r["last_input"],
                "context_resets": r["context_resets"],
                "total": tt(r["totals"]),
                "large_context_small_output": tt(r["large_small"]),
            } for r in context_rows
        ],
        "large_context_small_output_by_role": {k: tt(v) for k, v in sorted(large_by_role.items())},
        "large_context_small_output_root_by_active_state": {k: tt(v) for k, v in sorted(large_root_state.items())},
    }
    if compaction is not None:
        obj["compaction_audit"] = {
            "explicit_compactions": compaction.explicit_count,
            "direct_usage_matched": compaction.direct_matched,
            "direct_usage_already_in_primary_totals": compaction.direct_in_primary_count,
            "direct_usage_coverage": (
                compaction.direct_matched / compaction.explicit_count
                if compaction.explicit_count else None
            ),
            "direct_compaction_usage": tt(compaction.direct_totals),
            "direct_compaction_usage_outside_primary_totals": tt(compaction.direct_outside_primary_totals),
            "post_compaction_recovery": tt(compaction.recovery_totals),
            "same_session_non_recovery_baseline": tt(compaction.baseline_totals),
            "recovery_tool_events": compaction.recovery_tool_events,
            "baseline_tool_events": compaction.baseline_tool_events,
            "baseline_comparison": compaction.baseline,
            "old_context_drop_heuristic": {
                "heuristic_events": compaction.heuristic_count,
                "matched_to_explicit": compaction.heuristic_matched,
                "explicit_only": compaction.explicit_only,
                "heuristic_only": compaction.heuristic_only,
            },
            "parse_errors": compaction.parse_errors,
            "by_role": {
                role: {
                    "compactions": row["compactions"],
                    "direct_usage_matched": row["direct_matched"],
                    "direct_usage": tt(row["direct"]),
                    "recovery": tt(row["recovery"]),
                    "runtime_hours": row["runtime_hours"],
                    "compactions_per_hour": row["compactions_per_hour"],
                    "median_gap_seconds": row["median_gap_seconds"],
                    "median_observed_shrink_fraction": row["median_shrink_fraction"],
                    "repeated_read_events": row["repeated_read_events"],
                }
                for role, row in sorted(compaction.by_role.items())
            },
            "events": [
                {
                    "index": e.index,
                    "agent": e.agent_label,
                    "role": e.role,
                    "timestamp": e.ts.isoformat(),
                    "completion_timestamp": e.completion_ts.isoformat(),
                    "marker_kinds": list(e.marker_kinds),
                    "direct_usage_observed": e.direct_usage is not None,
                    "direct_usage_method": e.direct_usage_method,
                    "direct_usage_confidence": e.direct_usage_confidence,
                    "direct_usage_in_primary_totals": e.direct_usage_in_primary_totals,
                    "direct_usage": (
                        {
                            "input_tokens": e.direct_usage.input_tokens,
                            "cached_input_tokens": e.direct_usage.cached_input_tokens,
                            "uncached_input_tokens": e.direct_usage.uncached_input_tokens,
                            "output_tokens": e.direct_usage.output_tokens,
                            "reasoning_output_tokens": e.direct_usage.reasoning_tokens,
                            "model": e.direct_usage.model,
                            "effort": e.direct_usage.effort,
                            "api_list_equivalent_usd": round(e.direct_api_eq, 6) if e.direct_api_eq is not None else None,
                        }
                        if e.direct_usage is not None else None
                    ),
                    "context_before_input_tokens": e.before_input_tokens,
                    "context_after_first_request_input_tokens": e.after_input_tokens,
                    "observed_shrink_fraction": e.shrink_fraction,
                    "refill_target_tokens": e.refill_target_tokens,
                    "refill_reached": e.refill_reached,
                    "recovery_end": e.recovery_end.isoformat() if e.recovery_end else None,
                    "recovery_end_reason": e.recovery_end_reason,
                    "recovery_duration_seconds": e.recovery_duration_seconds,
                    "recovery_peak_input_tokens": e.recovery_peak_input_tokens,
                    "recovery": tt(e.recovery_totals),
                    "tool_activity": dict(e.tool_counts),
                    "tool_events": e.tool_events,
                    "unique_resources_after": e.unique_resources_after,
                    "pre_compaction_resources": e.pre_resources,
                    "repeated_resources": e.repeated_resources,
                    "repeated_resource_access_events": e.repeated_resource_access_events,
                    "repeated_read_resources": e.repeated_read_resources,
                    "repeated_read_events": e.repeated_read_events,
                }
                for e in compaction.events
            ],
            "notes": [
                "Direct compaction usage is reported only when a raw token_count sample can be conservatively associated with an explicit compaction marker.",
                "Recovery workload is observed after compaction until the next compaction, session end, or the configured refill threshold is reached; it is not automatically caused by compaction.",
                "Resource identities are one-way hashed internally and are not exported; only aggregate repeated-access counts are exported.",
                "Same-session baseline deltas are exploratory comparisons, not savings estimates.",
            ],
        }
    if schema is not None:
        obj["action_schema_audit"] = {
            "bridge_candidates": dict(schema.bridge_candidates),
            "paths": [
                {
                    "action": kind,
                    "side": side,
                    "path": ptxt,
                    "calls_present": len(st.calls_present),
                    "call_total": schema.call_totals.get((kind, side), 0),
                    "types": dict(st.types),
                    "distinct_compact_values": len(st.scalar_hashes),
                    "id_shaped_occurrences": st.id_shaped,
                    "family_id_matches": st.family_id_matches,
                    "spawn_result_value_overlap": st.spawn_handle_overlap,
                    "role_label_like_occurrences": st.role_label_hits,
                }
                for (kind, side, ptxt), st in sorted(schema.stats.items())
            ],
        }
    Path(path).write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def print_report(family: finder.Family,
                 sessions: Dict[str, finder.Session],
                 parsed: Dict[str, lifecycle.ParsedSession],
                 labels: Dict[str, str],
                 windows: Sequence[ActiveWindow],
                 role_totals: Dict[str, TokenTotals],
                 root_states: Dict[str, TokenTotals],
                 bursts: Dict[str, List[Burst]],
                 cycles: Sequence[Cycle],
                 context_rows: Sequence[dict],
                 large_by_role: Dict[str, TokenTotals],
                 large_root_state: Dict[str, TokenTotals],
                 root_child_counts: Dict[str, TokenTotals],
                 concurrency: dict,
                 overlap_agents: Sequence[dict],
                 overlap_pairs: Sequence[dict],
                 lingering: Sequence[LingeringExposure],
                 successor_map: Dict[str, str],
                 role_target_counts: Counter,
                 role_target_costs: Dict[str, TokenTotals],
                 role_target_unmatched: int,
                 schema: Optional[SchemaAudit],
                 analysis_meta: AnalysisMeta,
                 chunks: Sequence[dict],
                 comparison: dict,
                 compaction: Optional[CompactionAudit],
                 args: argparse.Namespace,
                 workflow_analysis: dict) -> None:
    all_total = TokenTotals()
    for t in role_totals.values():
        all_total.add_totals(t)
    root_total = role_totals.get(lifecycle.role_for_key(family.root, family, sessions), TokenTotals())

    trusted_spawns = trusted_spawn_actions(family, parsed)
    recognized_targets = {w.target_key for w in windows}
    role_children = [k for k in family.members if k != family.root]
    excluded = [k for k in role_children if k not in recognized_targets]

    print(f"\nWorkflow cost profile {family.family_key}")
    print("=" * (22 + len(family.family_key)))
    first, last = family.first_ts(sessions), family.last_ts(sessions)
    if analysis_meta.windowed:
        print("analysis window:")
        after_text = args.after or "-"
        before_text = args.before or "-"
        after_utc = analysis_meta.requested_after.isoformat() if analysis_meta.requested_after else "-"
        before_utc = analysis_meta.requested_before.isoformat() if analysis_meta.requested_before else "-"
        print(f"  after:  {after_text}  (UTC {after_utc})")
        print(f"  before: {before_text}  (UTC {before_utc})")
        if analysis_meta.source_family_start and analysis_meta.source_family_end:
            print(f"source family span: {local_time(analysis_meta.source_family_start)} -> {local_time(analysis_meta.source_family_end)}")
        print(f"analysis root: {family.root}  role={lifecycle.role_for_session(sessions[family.root])}  selection={analysis_meta.root_selection_method}/{analysis_meta.root_selection_confidence}")
        selected_evidence = next((r for r in analysis_meta.orchestrator_candidates if r.get("session_key") == family.root), None)
        if selected_evidence:
            print(f"structural root/subtree activity events: {selected_evidence['activity_events']}")
        print(f"segment membership: {analysis_meta.membership_method}; primary sessions={len(family.members)}")
        print(f"window session audit: pre={len(analysis_meta.pre_window)}, carry-in={len(analysis_meta.carry_in)}, "
              f"in-window={len(analysis_meta.in_window)}, post={len(analysis_meta.post_window)}, carry-out={len(analysis_meta.carry_out)}")
        if analysis_meta.excluded_in_window:
            print(f"in-window sessions outside selected segment: {len(analysis_meta.excluded_in_window)}")
        excluded_carry_in = [k for k in analysis_meta.carry_in if k != family.root]
        if family.root in analysis_meta.carry_in:
            print("selected orchestrator is a carry-in/resumed session; only in-window events are counted in primary totals")
        if excluded_carry_in:
            print(f"other carry-in activity after cutoff (excluded from primary totals): "
                  f"sessions={len(excluded_carry_in)}, req={analysis_meta.carry_in_totals.requests}, "
                  f"raw={fmt_tokens(analysis_meta.carry_in_totals.total_tokens)}, "
                  f"API$eq={fmt_eq(analysis_meta.carry_in_totals.api_eq)}")
    effective_first = analysis_meta.analysis_start or first
    effective_last = analysis_meta.analysis_end or last
    if effective_first and effective_last:
        print(f"analysis span: {local_time(effective_first)} -> {local_time(effective_last)} "
              f"({fmt_duration((effective_last-effective_first).total_seconds())})")
    print(f"sessions: {len(family.members)}")
    print(f"trusted spawns: {len(trusted_spawns)}; observed lifetime windows: {len(windows)}")
    print(f"sessions without a trusted lifetime window: {len(excluded)} (usage remains included)")
    print("Core activity covers all roles, including unknown roles, with a trusted spawn + observed lifetime.")
    print("Windows represent observed descendant lifetimes, not continuous execution or just immediate children.")
    print(f"Workflow interpretation: {args.workflow_profile}; optional role filter: {args.stage_roles or 'none'}")
    print(f"Lifetime-window coverage: {workflow_analysis['lifetime_windows_status']}")
    print("Exact role-targeted SENDs use configured role strings, not guessed session recipients.")
    print("API$eq is a public API-list-price normalization ruler, not billing.\n")

    print("Role / workflow cost")
    print("--------------------")
    print("role                             req      input   cached   uncached    output     API$eq     raw%     $eq%")
    for name, t in sorted(role_totals.items(), key=lambda kv: (kv[1].api_eq, kv[1].total_tokens), reverse=True):
        print_token_row(name, t, all_total.total_tokens, all_total.api_eq)
    print_token_row("TOTAL", all_total, all_total.total_tokens, all_total.api_eq)
    print(f"price coverage by observed tokens: {fmt_pct(all_total.price_coverage)}")

    print("\nRoot cost by observed descendant state")
    print("---------------------------------------")
    print("active state                      req      input   cached   uncached    output     API$eq   root raw%  root $eq%")
    for name, t in sorted(root_states.items(), key=lambda kv: (kv[1].api_eq, kv[1].total_tokens), reverse=True):
        print_token_row(name, t, root_total.total_tokens, root_total.api_eq, width=32)
    print("These states are mutually exclusive for each root inference request, so this table is additive.")

    print("\nRoot cost by overlapping descendant lifetimes")
    print("-------------------------------------------")
    print("children                         req      input   cached   uncached    output     API$eq   root $eq%")
    root_total_eq = sum(t.api_eq for t in root_child_counts.values())
    for bucket in ("0", "1", "2", "3+"):
        t = root_child_counts.get(bucket, TokenTotals())
        if not t.requests and bucket not in root_child_counts:
            continue
        share = t.api_eq / root_total_eq if root_total_eq else float("nan")
        label = {"0": "0 active", "1": "1 active", "2": "2 active", "3+": "3+ active"}[bucket]
        print(f"{label:<30} {t.requests:>6} {fmt_tokens(t.input_tokens):>10} {fmt_pct(t.cache_share):>7} "
              f"{fmt_tokens(t.uncached_input_tokens):>10} {fmt_tokens(t.output_tokens):>9} {fmt_eq(t.api_eq):>10} {fmt_pct(share):>10}")
    concurrent_root = TokenTotals()
    for bucket in ("2", "3+"):
        if bucket in root_child_counts:
            concurrent_root.add_totals(root_child_counts[bucket])
    print(f"root work with 2+ observed descendants active: {fmt_eq(concurrent_root.api_eq)} "
          f"({fmt_pct(concurrent_root.api_eq / root_total_eq if root_total_eq else float('nan'))} of root API$eq)")

    print("\nConcurrency exposure")
    print("--------------------")
    print(f"observed descendant agent-minutes: {concurrency['agent_seconds']/60.0:.1f} "
          f"({concurrency['agent_seconds']/3600.0:.2f} agent-hours)")
    print(f"wall time with >=1 observed descendant: {concurrency['active_wall_seconds']/60.0:.1f}m")
    print(f"wall time with >=2 observed descendants: {concurrency['overlap_wall_seconds']/60.0:.1f}m")
    print(f"extra concurrency-hours beyond one child: {concurrency['extra_concurrency_seconds']/3600.0:.2f}h")
    print(f"peak simultaneous observed descendants: {concurrency['peak_concurrent_children']}")
    filtered = workflow_analysis["role_filtered_activity"]
    if filtered is not None:
        print(f"Optional role-filtered view ({', '.join(filtered['roles'])}): "
              f"{len(filtered['agents'])} observed agents; "
              f"peak lifetime overlap={filtered['concurrency']['peak_concurrent_children']}. "
              "Core totals and concurrency above are unfiltered.")
    if overlap_agents:
        print("Agents with the most time overlapping another observed descendant:")
        for r in overlap_agents[:min(args.agent_limit, 10)]:
            if r['overlap_seconds'] <= 0:
                continue
            names = ",".join(r['overlaps_with'][:4])
            if len(r['overlaps_with']) > 4:
                names += f",+{len(r['overlaps_with'])-4}"
            print(f"  {r['agent']:<5} {r['role']:<12} overlap={fmt_duration(r['overlap_seconds']):>7} "
                  f"of {fmt_duration(r['duration_seconds']):>7} ({fmt_pct(r['overlap_fraction']):>4}) with {names}")
    if overlap_pairs:
        print("Longest pairwise overlaps:")
        for r in overlap_pairs[:min(args.agent_limit, 10)]:
            print(f"  {r['agent_a']}({r['role_a']}) + {r['agent_b']}({r['role_b']}): "
                  f"{fmt_duration(r['overlap_seconds'])}")

    print("\nRecognized active windows")
    print("-------------------------")
    if not windows:
        print("Unavailable: no trusted descendant lifetime windows; this does not prove no worker activity.")
    else:
        print("#   start                end                  role          agent chunk duration  overlaps  root API$eq  child API$eq")
        for w in windows[:args.stage_limit]:
            rt = aggregate_requests(request_slice(parsed, [family.root], w.start, w.end), args._prices)
            ct = aggregate_requests(request_slice(parsed, [w.target_key], w.start, w.end), args._prices)
            overlaps = sum(1 for x in windows if x.target_key != w.target_key and x.start < w.end and x.end > w.start)
            print(
                f"{w.index:<3} {local_time(w.start):<20} {local_time(w.end):<20} "
                f"{w.role:<13} {w.target_label:<5} {(w.chunk_label or '-'):>5} {fmt_duration(w.duration_seconds):>8} "
                f"{overlaps:>8} {fmt_eq(rt.api_eq):>11} {fmt_eq(ct.api_eq):>12}"
            )
        if len(windows) > args.stage_limit:
            print(f"... {len(windows)-args.stage_limit} more active windows")
        print("Per-window root costs can overlap and must not be summed when windows overlap.")

    print("\nChunk / assignment correlation")
    print("------------------------------")
    correlated = sum(1 for w in windows if w.chunk_label)
    coverage = correlated / len(windows) if windows else float("nan")
    print(f"trusted active windows: {len(windows)}; chunk-correlated: {correlated}; "
          f"coverage={fmt_pct(coverage)}; correlated chunks={len(chunks)}")
    print("Correlation requires an explicit structured spawn task label like <chunk-id>:<role>:<attempt>; timing alone is never used.")
    if chunks:
        print("chunk  assignments roles                         direct child API$eq  root during windows")
        for r in chunks[:args.stage_limit]:
            roles_text = ",".join(f"{k}:{v}" for k, v in sorted(r["roles"].items()))
            print(f"{r['chunk']:<6} {r['assignments']:>11} {roles_text[:28]:<28} "
                  f"{fmt_eq(r['child'].api_eq):>19} {fmt_eq(r['root_during_chunk_windows'].api_eq):>20}")
        if len(chunks) > args.stage_limit:
            print(f"... {len(chunks)-args.stage_limit} more correlated chunks")
    else:
        print("No strong chunk correlation available in this sample. Overlap remains unclassified rather than inferred from timing.")

    print("\nLifecycle overlap classification")
    print("--------------------------------")
    print("Older workers are compared with later same-role or configured successor-role spawns while both are alive.")
    print("Same-chunk repair/replacement is descriptive; cross-chunk overlap is the primary lifecycle optimization signal.")
    print("agent role          trigger               class                           linger   child req  child API$eq  root API$eq")
    if not lingering:
        print("No lifecycle overlap candidates found under the configured successor map.")
    else:
        for r in lingering[:args.agent_limit]:
            trigger = f"{r.trigger_window.target_label}({r.trigger_window.role})"
            print(f"{r.window.target_label:<5} {r.window.role:<13} {trigger:<21} {r.classification:<31} "
                  f"{fmt_duration(r.lingering_seconds):>7} {r.child_after_trigger.requests:>10} "
                  f"{fmt_eq(r.child_after_trigger.api_eq):>13} {fmt_eq(r.root_during_lingering.api_eq):>12}")
        by_class = exposure_totals_by_class(lingering)
        for cls in ("cross-chunk-overlap", "same-chunk-repair-overlap", "same-chunk-replacement-overlap", "unclassified-overlap"):
            t = by_class.get(cls, TokenTotals())
            n = sum(1 for r in lingering if r.classification == cls and r.active_after_trigger)
            if n or t.requests:
                print(f"  {cls:<31} active workers={n:<3} child API$eq={fmt_eq(t.api_eq):>9} raw={fmt_tokens(t.total_tokens):>8}")
        print("successor map: " + ", ".join(f"{k}->{v}" for k, v in sorted(successor_map.items())))

    print("\nExact role-targeted root SENDs")
    print("------------------------------")
    total_role_sends = sum(role_target_counts.values())
    if not total_role_sends:
        print("No root SEND arguments exactly named a configured role.")
    else:
        print("role                 calls  paired inference   input     API$eq   root $eq%")
        for role, calls in role_target_counts.most_common():
            t = role_target_costs.get(role, TokenTotals())
            share = t.api_eq / root_total.api_eq if root_total.api_eq else float("nan")
            print(f"{role:<20} {calls:>5} {t.requests:>16} {fmt_tokens(t.input_tokens):>8} {fmt_eq(t.api_eq):>10} {fmt_pct(share):>10}")
        print(f"exact role-targeted SEND calls: {total_role_sends}; inference pairings missing: {role_target_unmatched}")
        print("Role target is trusted. Nearby inference pairing is heuristic and is not a causal cost claim.")

    print("\nConfigured role-pair cycles")
    print("------------------------------------")
    if not args.cycle_roles:
        print("Unavailable: no role-pair interpretation configured; use --cycle-roles or --workflow-profile staged.")
    elif not cycles:
        print(f"Unavailable: no supported consecutive {args.cycle_roles[0]} -> {args.cycle_roles[1]} transitions found.")
    else:
        print(f"Roles: {args.cycle_roles[0]} -> {args.cycle_roles[1]}")
        print("#  first second quality            handoff   other active agents        first agent root/first second agent root/second ratio")
        for c in cycles[:args.cycle_limit]:
            ratio = c.supervision_ratio_api_eq if c.ratio_eligible else float("nan")
            ratio_text = f"{ratio:.2f}x" if math.isfinite(ratio) else "-"
            other = ",".join(c.other_active_agents) if c.other_active_agents else "none"
            print(
                f"{c.index:<2} {c.implementer_window.target_label:<5} {c.validator_window.target_label:<5} "
                f"{c.quality:<19} {c.handoff_gap_seconds:>7.1f}s {other[:25]:<25} "
                f"{fmt_eq(c.implementer_agent.api_eq):>10} {fmt_eq(c.root_implementer.api_eq):>10} "
                f"{fmt_eq(c.validator_agent.api_eq):>10} {fmt_eq(c.root_validator.api_eq):>9} {ratio_text:>11}"
            )
        if len(cycles) > args.cycle_limit:
            print(f"... {len(cycles)-args.cycle_limit} more cycles")

    supervision = _cycle_supervision_summary(cycles)
    iso = supervision["isolated_sequential"]
    print("\nSequential-cycle supervision summary")
    print("------------------------------------")
    print(f"sequential cycles observed: {supervision['sequential_cycles_observed']}; "
          f"isolated ratio-eligible cycles: {iso['cycles']}")
    if iso["cycles"]:
        ir = iso["first_role_ratio_api_eq"]
        vr = iso["second_role_ratio_api_eq"]
        cr = iso["combined_supervision_ratio_api_eq"]
        rr = iso["combined_supervision_ratio_raw"]
        print(f"isolated root/first-role API$eq:            {ir:.2f}x" if ir is not None else "isolated first-role ratio: -")
        print(f"isolated root/second-role API$eq:           {vr:.2f}x" if vr is not None else "isolated second-role ratio: -")
        print(f"isolated combined root/direct-child API$eq: {cr:.2f}x" if cr is not None else "isolated combined ratio: -")
        print(f"isolated combined root/direct-child raw:    {rr:.2f}x" if rr is not None else "isolated combined raw ratio: -")
        print("These are observational concurrency measures, not guaranteed avoidable overhead.")
    else:
        print("No overall supervision ratio: no configured, complete sequential cycle is isolated from other observed descendants.")
    suppressed_overlap = sum(c.quality == "overlap" for c in cycles)
    suppressed_noniso = supervision['ratios_suppressed_for_nonisolated_cycles']
    if suppressed_overlap or suppressed_noniso:
        print(f"ratios suppressed: overlap cycles={suppressed_overlap}, non-isolated sequential cycles={suppressed_noniso}")

    print("\nAgent re-entry / inference bursts")
    print("---------------------------------")
    print(f"A new burst starts after >{args.burst_gap_seconds:.0f}s with no inference in that session.")
    print("agent role                     bursts  total API$eq  after-1st API$eq  after-1st raw  peak context")
    burst_rows = []
    for key in family.members:
        if key == family.root:
            continue
        bs = bursts.get(key, [])
        if not bs:
            continue
        total = TokenTotals()
        reentry = TokenTotals()
        for i, b in enumerate(bs):
            total.add_totals(b.totals)
            if i > 0:
                reentry.add_totals(b.totals)
        role = lifecycle.role_for_key(key, family, sessions)
        peak = max(b.peak_input for b in bs)
        burst_rows.append((reentry.api_eq, reentry.total_tokens, total.api_eq, labels[key], role, len(bs), reentry, peak))
    burst_rows.sort(reverse=True)
    for _, _, total_eq, label, role, n, reentry, peak in burst_rows[:args.agent_limit]:
        print(
            f"{label:<5} {role:<24} {n:>6} {fmt_eq(total_eq):>13} "
            f"{fmt_eq(reentry.api_eq):>17} {fmt_tokens(reentry.total_tokens):>14} {fmt_tokens(peak):>13}"
        )

    print("\nLarge-context / small-output workload")
    print("-------------------------------------")
    print(f"threshold: input >= {fmt_tokens(args.large_context_input_tokens)}, output <= {fmt_tokens(args.small_output_tokens)}")
    large_total = TokenTotals()
    for t in large_by_role.values():
        large_total.add_totals(t)
    print(
        f"all roles: requests={large_total.requests:,}, input={fmt_tokens(large_total.input_tokens)}, "
        f"cached={fmt_pct(large_total.cache_share)}, output={fmt_tokens(large_total.output_tokens)}, "
        f"API$eq={fmt_eq(large_total.api_eq)} ({fmt_pct(large_total.api_eq / all_total.api_eq if all_total.api_eq else float('nan'))} of workflow $eq)"
    )
    for role, t in sorted(large_by_role.items(), key=lambda kv: kv[1].api_eq, reverse=True):
        print(f"  {role:<26} req={t.requests:<4} input={fmt_tokens(t.input_tokens):>8} API$eq={fmt_eq(t.api_eq):>9}")
    if large_root_state:
        print("  root subset by observed descendant state:")
        for state, t in sorted(large_root_state.items(), key=lambda kv: kv[1].api_eq, reverse=True):
            print(f"    {state:<32} req={t.requests:<4} input={fmt_tokens(t.input_tokens):>8} API$eq={fmt_eq(t.api_eq):>9}")

    print("\nContext growth / reread candidates")
    print("----------------------------------")
    print("agent role                     req   first   median      p90     peak   drops*  large/small API$eq")
    for r in context_rows[:args.agent_limit]:
        print(
            f"{r['label']:<5} {r['role']:<24} {r['requests']:>4} "
            f"{fmt_tokens(r['first_input']):>7} {fmt_tokens(r['median_input']):>8} "
            f"{fmt_tokens(r['p90_input']):>8} {fmt_tokens(r['peak_input']):>8} "
            f"{r['context_resets']:>8} {fmt_eq(r['large_small'].api_eq):>20}"
        )
    print("* drops is the legacy large-context -> <55% next-request heuristic, not an explicit compaction count")

    if compaction is not None:
        print("\nContext compaction audit")
        print("------------------------")
        coverage = (
            compaction.direct_matched / compaction.explicit_count
            if compaction.explicit_count else float("nan")
        )
        print(
            f"explicit compactions: {compaction.explicit_count}; "
            f"direct token usage conservatively matched: {compaction.direct_matched} "
            f"({fmt_pct(coverage)})"
        )
        print(
            f"matched direct compaction usage: req={compaction.direct_totals.requests}, "
            f"input={fmt_tokens(compaction.direct_totals.input_tokens)}, "
            f"uncached={fmt_tokens(compaction.direct_totals.uncached_input_tokens)}, "
            f"output={fmt_tokens(compaction.direct_totals.output_tokens)}, "
            f"API$eq={fmt_eq(compaction.direct_totals.api_eq)}"
        )
        if compaction.direct_matched:
            print(
                f"direct usage already present in primary request totals: {compaction.direct_in_primary_count}/"
                f"{compaction.direct_matched}; matched direct usage outside primary totals: "
                f"API$eq={fmt_eq(compaction.direct_outside_primary_totals.api_eq)}"
            )
        recovery_share = (
            compaction.recovery_totals.api_eq / all_total.api_eq
            if all_total.api_eq else float("nan")
        )
        print(
            f"post-compaction recovery windows: req={compaction.recovery_totals.requests}, "
            f"input={fmt_tokens(compaction.recovery_totals.input_tokens)}, "
            f"uncached={fmt_tokens(compaction.recovery_totals.uncached_input_tokens)}, "
            f"API$eq={fmt_eq(compaction.recovery_totals.api_eq)} "
            f"({fmt_pct(recovery_share)} of workflow $eq)"
        )
        print(
            "old context-drop heuristic: "
            f"events={compaction.heuristic_count}, matched={compaction.heuristic_matched}, "
            f"explicit-only={compaction.explicit_only}, heuristic-only={compaction.heuristic_only}"
        )
        if compaction.parse_errors:
            print(f"raw compaction/tool scan parse errors: {compaction.parse_errors}")

        if compaction.by_role:
            print("role                     compact   /hour  direct cov  median gap  shrink  recovery API$eq  repeat-read")
            for role, row in sorted(
                compaction.by_role.items(),
                key=lambda kv: (kv[1]["compactions"], kv[1]["recovery"].api_eq),
                reverse=True,
            ):
                role_cov = row["direct_matched"] / row["compactions"] if row["compactions"] else float("nan")
                cph = f"{row['compactions_per_hour']:.2f}" if row["compactions_per_hour"] is not None else "-"
                gap = fmt_duration(row["median_gap_seconds"]) if row["median_gap_seconds"] is not None else "-"
                shrink = fmt_pct(row["median_shrink_fraction"]) if row["median_shrink_fraction"] is not None else "-"
                print(
                    f"{role:<24} {row['compactions']:>7} {cph:>7} {fmt_pct(role_cov):>11} "
                    f"{gap:>11} {shrink:>7} {fmt_eq(row['recovery'].api_eq):>16} "
                    f"{row['repeated_read_events']:>12}"
                )

        if compaction.events:
            print("\nCompaction events")
            print("#   time                 agent role                  before   after  shrink  direct $  recovery req  recovery $  refill  repeat-read")
            for e in compaction.events[:args.compaction_limit]:
                before = fmt_tokens(e.before_input_tokens) if e.before_input_tokens is not None else "-"
                after = fmt_tokens(e.after_input_tokens) if e.after_input_tokens is not None else "-"
                shrink = fmt_pct(e.shrink_fraction) if e.shrink_fraction is not None else "-"
                direct_eq = fmt_eq(e.direct_api_eq) if e.direct_api_eq is not None else "-"
                refill = "yes" if e.refill_reached else e.recovery_end_reason
                print(
                    f"{e.index:<3} {local_time(e.ts):<20} {e.agent_label:<5} {e.role:<21} "
                    f"{before:>7} {after:>7} {shrink:>7} {direct_eq:>9} "
                    f"{e.recovery_totals.requests:>12} {fmt_eq(e.recovery_totals.api_eq):>11} "
                    f"{refill[:15]:>15} {e.repeated_read_events:>12}"
                )
            if len(compaction.events) > args.compaction_limit:
                print(f"... {len(compaction.events)-args.compaction_limit} more compactions")
            print(
                f"resource lookback: {args.compaction_resource_lookback_minutes:g}m before each compaction; "
                "resource identities are one-way hashed and never printed/exported"
            )

        b = compaction.baseline
        print("\nPost-compaction recovery vs same-session non-recovery baseline")
        print("------------------------------------------------------------")
        if not b or not b.get("recovery_requests") or not b.get("baseline_requests"):
            print("Insufficient recovery/non-recovery requests for a same-session baseline comparison.")
        else:
            def _ratio_text(a: Optional[float], c: Optional[float]) -> str:
                if a is None or c in (None, 0):
                    return "-"
                return f"{a / c:.2f}x"

            print("metric                         recovery     baseline    ratio")
            print(
                f"API$eq / request               {b['recovery_api_eq_per_request']:>10.4f} "
                f"{b['baseline_api_eq_per_request']:>12.4f} "
                f"{_ratio_text(b['recovery_api_eq_per_request'], b['baseline_api_eq_per_request']):>8}"
            )
            print(
                f"input / request                {fmt_tokens(int(b['recovery_input_per_request'])):>10} "
                f"{fmt_tokens(int(b['baseline_input_per_request'])):>12} "
                f"{_ratio_text(b['recovery_input_per_request'], b['baseline_input_per_request']):>8}"
            )
            print(
                f"uncached input / request       {fmt_tokens(int(b['recovery_uncached_input_per_request'])):>10} "
                f"{fmt_tokens(int(b['baseline_uncached_input_per_request'])):>12} "
                f"{_ratio_text(b['recovery_uncached_input_per_request'], b['baseline_uncached_input_per_request']):>8}"
            )
            print(
                f"tool events / request          {b['recovery_tool_events_per_request']:>10.3f} "
                f"{b['baseline_tool_events_per_request']:>12.3f} "
                f"{_ratio_text(b['recovery_tool_events_per_request'], b['baseline_tool_events_per_request']):>8}"
            )
            api_delta = b.get("api_eq_delta_vs_same_session_baseline")
            input_delta = b.get("input_token_delta_vs_same_session_baseline")
            if api_delta is not None:
                print(f"aggregate API$eq delta vs baseline expectation: {api_delta:+.2f}")
            if input_delta is not None:
                sign = "+" if input_delta >= 0 else "-"
                print(f"aggregate input-token delta vs baseline expectation: {sign}{fmt_tokens(abs(int(input_delta)))}")
            print("Baseline deltas are exploratory workload comparisons, not causal compaction overhead or savings estimates.")

    signals = []
    for name, t in role_totals.items():
        signals.append((t.api_eq, f"role total: {name}", t))
    for name, t in root_states.items():
        signals.append((t.api_eq, f"root while {name}", t))
    signals.append((large_total.api_eq, "large-context/small-output workload", large_total))
    if compaction is not None and compaction.recovery_totals.requests:
        signals.append((compaction.recovery_totals.api_eq, "post-compaction recovery workload", compaction.recovery_totals))
    if compaction is not None and compaction.direct_totals.requests:
        signals.append((compaction.direct_totals.api_eq, "direct compaction usage (matched events only)", compaction.direct_totals))
    concurrent_root = TokenTotals()
    for bucket in ("2", "3+"):
        if bucket in root_child_counts:
            concurrent_root.add_totals(root_child_counts[bucket])
    if concurrent_root.requests:
        signals.append((concurrent_root.api_eq, "root with 2+ observed descendants active", concurrent_root))
    for role in sorted({lifecycle.role_for_key(k, family, sessions) for k in family.members if k != family.root}):
        re = TokenTotals()
        for key in family.members:
            if lifecycle.role_for_key(key, family, sessions) != role:
                continue
            for b in bursts.get(key, [])[1:]:
                re.add_totals(b.totals)
        if re.requests:
            signals.append((re.api_eq, f"{role} activity after first inference burst", re))
    for r in lingering:
        if not r.child_after_trigger.requests:
            continue
        if r.classification == "cross-chunk-overlap":
            label = f"cross-chunk active {r.window.target_label} after {r.trigger_window.target_label} spawn"
        elif r.classification == "unclassified-overlap":
            label = f"unclassified overlap {r.window.target_label} after {r.trigger_window.target_label} spawn"
        else:
            continue
        signals.append((r.child_after_trigger.api_eq, label, r.child_after_trigger))
    signals.sort(key=lambda x: x[0], reverse=True)

    print("\nComparison snapshot")
    print("-------------------")
    def cpct(name: str, value: object) -> None:
        if value is None:
            txt = "-"
        elif isinstance(value, float) and ("share" in name or "coverage" in name):
            txt = fmt_pct(value)
        elif isinstance(value, (int, float)) and name.endswith("_tokens"):
            txt = fmt_tokens(int(value))
        elif isinstance(value, float):
            txt = f"{value:.3f}"
        else:
            txt = str(value)
        print(f"{name:<46} {txt}")
    for name in (
        "root_api_eq_share", "root_requests",
        "root_median_input_tokens", "root_p90_input_tokens",
        "large_context_small_output_api_eq_share", "root_with_2plus_children_api_eq_share",
        "recognized_child_agent_hours", "extra_concurrency_hours", "peak_concurrent_children",
        "chunk_correlation_coverage", "correlated_chunks",
        "direct_child_requests_per_correlated_chunk", "direct_child_api_eq_per_correlated_chunk",
        "cross_chunk_overlap_api_eq", "same_chunk_repair_overlap_api_eq",
        "same_chunk_replacement_overlap_api_eq", "unclassified_overlap_api_eq",
        "explicit_compactions", "direct_compaction_usage_coverage", "direct_compaction_api_eq",
        "direct_compaction_api_eq_outside_primary_totals",
        "post_compaction_recovery_api_eq", "post_compaction_recovery_api_eq_share",
        "post_compaction_recovery_input_tokens", "post_compaction_recovery_requests",
        "root_compactions", "root_compactions_per_hour",
        "repeated_post_compaction_read_events", "recovery_api_eq_delta_vs_same_session_baseline",
    ):
        cpct(name, comparison.get(name))
    print("These stable fields are intended for before/after comparison; they are descriptive, not causal savings estimates.")

    print("\nPotential optimization opportunities")
    print("------------------------------------")
    print("Observed exposure only. These views overlap, are not additive, and are not estimates of achievable savings.")
    for eq, name, t in signals[:12]:
        share = eq / all_total.api_eq if all_total.api_eq else float("nan")
        print(f"{name:<64} API$eq={fmt_eq(eq):>10}  share={fmt_pct(share):>5}  raw={fmt_tokens(t.total_tokens):>8}")

    if schema is not None:
        print_schema_audit(schema, args.schema_limit)

def self_test() -> None:
    reset = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = finder.Session("/tmp/root", "S-root", first_ts=reset, last_ts=reset + timedelta(minutes=30), source_kind="cli")
    imp = finder.Session("/tmp/imp", "S-imp", first_ts=reset + timedelta(seconds=10), last_ts=reset + timedelta(minutes=10), source_kind="subagent")
    val = finder.Session("/tmp/val", "S-val", first_ts=reset + timedelta(minutes=9), last_ts=reset + timedelta(minutes=20), source_kind="subagent")
    imp.roles.self_role["implementer"] = 2
    val.roles.self_role["validator"] = 2
    sessions = {s.session_key: s for s in (root, imp, val)}
    fam = finder.Family(members=list(sessions), root="S-root", edges=[], family_key="W-test")
    parsed = {k: lifecycle.ParsedSession() for k in sessions}
    parsed["S-root"].actions = [
        lifecycle.ActionEvent(reset + timedelta(seconds=9), "S-root", "spawn", "spawn_agent", "c1", matched_session="S-imp", match_method="tight-start-time", match_confidence="medium"),
        lifecycle.ActionEvent(reset + timedelta(minutes=8, seconds=59), "S-root", "spawn", "spawn_agent", "c2", matched_session="S-val", match_method="tight-start-time", match_confidence="medium"),
    ]
    labels = {"S-root": "ROOT", "S-imp": "A01", "S-val": "A02"}
    windows, excluded = build_active_windows(fam, sessions, parsed, labels, {"implementer", "validator"}, 0)
    assert not excluded
    assert [w.role for w in windows] == ["implementer", "validator"], windows
    assert active_state_label(reset + timedelta(minutes=9, seconds=30), windows) == "implementer+validator"
    assert active_state_label(reset + timedelta(minutes=15), windows) == "validator only"
    cm = concurrency_metrics(windows, reset, reset + timedelta(minutes=30))
    assert cm["peak_concurrent_children"] == 2
    assert cm["overlap_wall_seconds"] > 0
    root_count = root_child_count_costs(fam, parsed, windows, audit.DEFAULT_PRICES)
    assert isinstance(root_count, dict)
    succ = {"implementer": "validator", "validator": "implementer"}
    parsed["S-imp"].usage = [
        lifecycle.UsageRequest(reset + timedelta(minutes=9, seconds=30), "S-imp", 100, 50, 10, 0, "gpt-6-astra", "high")
    ]
    parsed["S-root"].usage = [
        lifecycle.UsageRequest(reset + timedelta(minutes=9, seconds=30), "S-root", 100, 50, 10, 0, "gpt-6-astra", "high")
    ]
    ling = build_lingering_exposures(fam, parsed, windows, audit.DEFAULT_PRICES, succ)
    assert ling and ling[0].window.target_key == "S-imp"
    assert ling[0].child_after_trigger.requests == 1

    reqs = [
        lifecycle.UsageRequest(reset, "S-imp", 100, 50, 10, 0, "gpt-6-astra", "high"),
        lifecycle.UsageRequest(reset + timedelta(seconds=30), "S-imp", 120, 60, 10, 0, "gpt-6-astra", "high"),
        lifecycle.UsageRequest(reset + timedelta(minutes=5), "S-imp", 140, 70, 10, 0, "gpt-6-astra", "high"),
    ]
    bs = build_bursts_for_session("S-imp", "A01", "implementer", reqs, 120, audit.DEFAULT_PRICES)
    assert len(bs) == 2, bs
    assert bs[0].totals.requests == 2 and bs[1].totals.requests == 1

    assert exact_role_target_from_args({"target": "validator"}, finder.DEFAULT_ROLES) == "validator"
    assert exact_role_target_from_args({"target": "please ask the validator"}, finder.DEFAULT_ROLES) is None

    # v4: timezone-aware cutoff parsing and carry-in classification.
    cutoff = parse_aware_iso8601("2026-01-01T00:05:00+00:00", "--after")
    assert cutoff == reset + timedelta(minutes=5)
    try:
        parse_aware_iso8601("2026-01-01T00:05:00", "--after")
        raise AssertionError("naive cutoff should fail")
    except ValueError:
        pass
    meta = classify_sessions_for_window(fam, sessions, parsed, cutoff, None)
    assert "S-root" in meta.carry_in and "S-val" in meta.in_window

    # v4: post-cutoff orchestrator selection prefers a CLI session with trusted stage spawns.
    newroot = finder.Session("/tmp/newroot", "S-newroot", first_ts=reset + timedelta(minutes=6),
                             last_ts=reset + timedelta(minutes=25), source_kind="cli")
    imp2 = finder.Session("/tmp/imp2", "S-imp2", first_ts=reset + timedelta(minutes=7),
                          last_ts=reset + timedelta(minutes=12), source_kind="subagent")
    imp2.roles.self_role["implementer"] = 2
    sessions2 = {**sessions, "S-newroot": newroot, "S-imp2": imp2}
    fam2 = finder.Family(members=list(sessions2), root="S-root", edges=[], family_key="W-test2")
    parsed2 = {k: lifecycle.ParsedSession() for k in sessions2}
    parsed2["S-newroot"].actions = [
        lifecycle.ActionEvent(reset + timedelta(minutes=6, seconds=59), "S-newroot", "spawn",
                              "spawn_agent", "c3", matched_session="S-imp2",
                              match_method="tight-start-time", match_confidence="medium"),
        lifecycle.ActionEvent(reset + timedelta(minutes=8), "S-newroot", "spawn",
                              "spawn_agent", "c4", matched_session="S-val",
                              match_method="tight-start-time", match_confidence="medium"),
    ]
    selected, method, conf, candidates = select_analysis_root(
        fam2, sessions2, parsed2, cutoff, None, None
    )
    assert selected == "S-newroot" and conf in {"high", "medium"}, (selected, method, conf, candidates)

    # v4.1: old recorded first_ts must not disqualify a resumed orchestrator,
    # and a forked child with inherited history is classified by trusted spawn.
    resumed = finder.Session("/tmp/resumed", "S-resumed", first_ts=reset,
                             last_ts=reset + timedelta(minutes=20), source_kind="cli")
    forked = finder.Session("/tmp/forked", "S-forked", first_ts=reset,
                            last_ts=reset + timedelta(minutes=14), source_kind="subagent")
    forked.roles.self_role["implementer"] = 2
    sessions3 = {"S-resumed": resumed, "S-forked": forked}
    fam3 = finder.Family(members=list(sessions3), root="S-resumed", edges=[], family_key="W-v41")
    parsed3 = {k: lifecycle.ParsedSession() for k in sessions3}
    parsed3["S-resumed"].actions = [
        lifecycle.ActionEvent(reset + timedelta(minutes=7), "S-resumed", "spawn",
                              "spawn_agent", "c5", matched_session="S-forked",
                              match_method="tight-start-time", match_confidence="medium")
    ]
    parsed3["S-resumed"].usage = [
        lifecycle.UsageRequest(reset + timedelta(minutes=8), "S-resumed", 1000, 900, 10, 0, "gpt-6-astra", "high")
    ]
    parsed3["S-forked"].usage = [
        lifecycle.UsageRequest(reset + timedelta(minutes=9), "S-forked", 1000, 900, 10, 0, "gpt-6-astra", "high")
    ]
    meta3 = classify_sessions_for_window(fam3, sessions3, parsed3, cutoff, None)
    assert "S-resumed" in meta3.carry_in
    assert "S-forked" in meta3.in_window and "S-forked" not in meta3.carry_in
    selected3, method3, conf3, candidates3 = select_analysis_root(
        fam3, sessions3, parsed3, cutoff, None, None
    )
    assert selected3 == "S-resumed" and conf3 in {"high", "medium"}, (selected3, method3, conf3, candidates3)
    view3 = build_analysis_family(fam3, sessions3, parsed3, meta3, selected3)
    assert set(view3.members) == {"S-resumed", "S-forked"}
    parsed3w = filter_parsed_for_window(view3, parsed3, meta3.analysis_start, meta3.analysis_end)
    labels3 = lifecycle.assign_agent_labels(view3, sessions3, parsed3w)
    windows3, excluded3 = build_active_windows(
        view3, sessions3, parsed3w, labels3, {"implementer"}, 0, meta3.analysis_start, meta3.analysis_end
    )
    assert not excluded3 and len(windows3) == 1
    assert windows3[0].start == reset + timedelta(minutes=7)

    # v4: explicit assignment labels correlate chunks; timing/prose do not.
    a1 = structured_assignment_from_task_name("chunk-04:implementer:1", finder.DEFAULT_ROLES)
    a2 = structured_assignment_from_task_name("chunk-04:validator:1", finder.DEFAULT_ROLES)
    a3 = structured_assignment_from_task_name("please validate chunk 04", finder.DEFAULT_ROLES)
    assert a1 and a2 and a1[0] == a2[0] and a3 is None
    w_imp = ActiveWindow(1, "implementer", "S-i", "A10", reset, reset, reset + timedelta(minutes=5), "x", "high",
                         chunk_key=a1[0], chunk_label="C01")
    w_val = ActiveWindow(2, "validator", "S-v", "A11", reset + timedelta(minutes=1), reset + timedelta(minutes=1),
                         reset + timedelta(minutes=6), "x", "high", chunk_key=a2[0], chunk_label="C01")
    cls = overlap_classification(w_imp, w_val, {"implementer": "validator"})
    assert cls and cls[1] == "same-chunk-repair-overlap"
    other = structured_assignment_from_task_name("chunk-05:implementer:1", finder.DEFAULT_ROLES)
    assert other
    w_next = ActiveWindow(3, "implementer", "S-n", "A12", reset + timedelta(minutes=2), reset + timedelta(minutes=2),
                          reset + timedelta(minutes=7), "x", "high", chunk_key=other[0], chunk_label="C02")
    cls2 = overlap_classification(w_imp, w_next, {"implementer": "validator"})
    assert cls2 and cls2[1] == "cross-chunk-overlap"

    # v4.2: explicit compaction markers are deduped, raw direct usage can be
    # recovered even when cumulative totals do not advance, and recovery tracks
    # refill plus repeated privacy-safe resource access.
    with tempfile.TemporaryDirectory() as td:
        rollout = Path(td) / "rollout.jsonl"
        def rec(ts: datetime, typ: str, payload: object) -> str:
            return json.dumps({"timestamp": ts.isoformat().replace("+00:00", "Z"), "type": typ, "payload": payload})

        def usage_payload(inp: int, cached: int, out: int, cumulative: int) -> dict:
            return {
                "type": "token_count",
                "model": "gpt-6-astra",
                "effort": "high",
                "info": {
                    "last_token_usage": {
                        "input_tokens": inp,
                        "cached_input_tokens": cached,
                        "output_tokens": out,
                        "reasoning_output_tokens": 0,
                        "total_tokens": inp + out,
                    },
                    "total_token_usage": {"total_tokens": cumulative},
                },
            }

        lines = [
            rec(reset + timedelta(minutes=1), "event_msg", usage_payload(200_000, 190_000, 1_000, 201_000)),
            rec(reset + timedelta(minutes=1, seconds=30), "response_item", {
                "type": "function_call", "name": "shell_command",
                "arguments": json.dumps({"command": "cat src/example.py"}), "call_id": "tool-1",
            }),
            rec(reset + timedelta(minutes=2), "compacted", {"replacement_history": []}),
            rec(reset + timedelta(minutes=2, milliseconds=10), "event_msg", usage_payload(198_000, 190_000, 5_000, 201_000)),
            rec(reset + timedelta(minutes=2, milliseconds=20), "event_msg", {"type": "context_compacted"}),
            rec(reset + timedelta(minutes=2, seconds=30), "response_item", {
                "type": "function_call", "name": "shell_command",
                "arguments": json.dumps({"command": "cat src/example.py"}), "call_id": "tool-2",
            }),
            rec(reset + timedelta(minutes=2, seconds=40), "event_msg", usage_payload(50_000, 45_000, 500, 251_500)),
            rec(reset + timedelta(minutes=3), "event_msg", usage_payload(100_000, 95_000, 500, 352_000)),
            rec(reset + timedelta(minutes=4), "event_msg", usage_payload(170_000, 160_000, 500, 522_500)),
            rec(reset + timedelta(minutes=6), "event_msg", usage_payload(60_000, 55_000, 500, 583_000)),
        ]
        rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cs = finder.Session(str(rollout), "S-cmp", first_ts=reset + timedelta(minutes=1),
                            last_ts=reset + timedelta(minutes=6), source_kind="cli")
        cfam = finder.Family(members=["S-cmp"], root="S-cmp", edges=[], family_key="W-cmp")
        cparsed = {"S-cmp": lifecycle.parse_family_session(str(rollout), "S-cmp", finder.DEFAULT_ROLES)}
        caudit = build_compaction_audit(
            cfam, {"S-cmp": cs}, cparsed, {"S-cmp": "ROOT"}, audit.DEFAULT_PRICES,
            None, None, dedupe_seconds=2.0, direct_usage_seconds=1.0,
            refill_fraction=0.80, resource_lookback_minutes=20.0,
            heuristic_input_threshold=150_000,
        )
        assert caudit.explicit_count == 1, caudit.explicit_count
        assert caudit.direct_matched == 1, caudit.direct_matched
        assert caudit.direct_in_primary_count == 0 and caudit.direct_outside_primary_totals.requests == 1
        ce = caudit.events[0]
        assert ce.direct_usage is not None and ce.direct_usage.input_tokens == 198_000
        assert ce.before_input_tokens == 200_000 and ce.after_input_tokens == 50_000
        assert ce.refill_reached and ce.recovery_totals.requests == 3
        assert ce.repeated_read_events == 1 and ce.repeated_read_resources == 1
        assert caudit.heuristic_matched == 1
    print("self-test: OK")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Profile token/API-equivalent work in a Codex session or linked session family.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 profile_workflow_cost.py --family W-e7af89b98f
  python3 profile_workflow_cost.py --family S-644a110a4f
  python3 profile_workflow_cost.py --family W-e7af89b98f --export-json workflow_cost_profile.json
  python3 profile_workflow_cost.py --family W-ac4e5c3e8a --after 2026-09-14T19:54:47+02:00
  python3 profile_workflow_cost.py --family W-e7af89b98f --compaction-refill-fraction 0.8
  python3 profile_workflow_cost.py --family W-e7af89b98f --no-action-schema-audit
  python3 profile_workflow_cost.py --family W-e7af89b98f --successor implementer=validator
  python3 profile_workflow_cost.py --self-test

Core activity covers every role with trusted spawn/lifetime evidence, including
unnamed workers. Role and assignment interpretation is optional. With date windows,
the profiler selects a structural root with subtree activity, requiring an explicit
--analysis-root if ambiguous. Non-root carry-ins remain separately reported.
Exact configured role names in SEND routing fields are trusted at the role level,
but unresolved SEND/WAIT calls are never assigned to a specific child session.
""",
    )
    ap.add_argument("--family", "--session", "--session-id", dest="family", metavar="ID",
                    help="family W-..., member S-..., or exact Codex session/thread ID")
    ap.add_argument("--home", default=os.path.expanduser("~/.codex"), help="Codex data directory")
    ap.add_argument("--after", metavar="ISO8601", help="analyze only the post-boundary segment; timezone offset is required")
    ap.add_argument("--before", metavar="ISO8601", help="optional exclusive end of analysis window; timezone offset is required")
    ap.add_argument("--analysis-root", "--orchestrator", dest="orchestrator", metavar="ID", help="override the analysis root with a hashed key or exact raw session ID")
    ap.add_argument("--workflow-profile", choices=("generic", "staged"), default="generic",
                    help="optional workflow interpretation; generic assumes no stage sequence (default)")
    ap.add_argument("--assignment-map", metavar="JSON", help="optional explicit workflow-assignment-map-v1 identities; no transcript parsing")
    ap.add_argument("--roles", nargs="+", default=list(finder.DEFAULT_ROLES), help="role labels to recognize")
    ap.add_argument("--stage-roles", nargs="+", help="optional role-filtered activity view; never filters core usage or concurrency")
    ap.add_argument("--cycle-roles", nargs=2, metavar=("FIRST", "SECOND"),
                    help="explicit role pair for sequential-cycle interpretation; staged defaults to implementer validator")
    ap.add_argument("--tight-spawn-seconds", type=float, default=2.0, help="trusted unique spawn/start temporal window")
    ap.add_argument("--spawn-window-minutes", type=float, default=30.0, help="broad diagnostic spawn window")
    ap.add_argument("--active-tail-seconds", "--stage-tail-seconds", dest="active_tail_seconds", type=float, default=0.0,
                    help="optional grace period after child session end for active-state analysis (default: 0)")
    ap.add_argument("--burst-gap-seconds", type=float, default=120.0, help="idle gap that starts a new inference burst")
    ap.add_argument("--cycle-handoff-seconds", type=float, default=300.0, help="max handoff gap labeled sequential")
    ap.add_argument("--successor", action="append", default=[], metavar="ROLE=ROLE",
                    help="next-stage relation for overlap interpretation; repeatable; staged supplies default relations")
    ap.add_argument("--action-inference-window-seconds", type=float, default=90.0,
                    help="window for observational pairing of role-targeted SENDs to nearby root inference")
    ap.add_argument("--large-context-input-tokens", type=int, default=200_000, help="large-context threshold")
    ap.add_argument("--small-output-tokens", type=int, default=2_000, help="small-output threshold")
    ap.add_argument("--compaction-refill-fraction", type=float, default=0.80,
                    help="end recovery when request input reaches this fraction of pre-compaction input (default: 0.80)")
    ap.add_argument("--compaction-resource-lookback-minutes", type=float, default=20.0,
                    help="pre-compaction lookback for privacy-safe repeated resource access detection (default: 20)")
    ap.add_argument("--compaction-dedupe-seconds", type=float, default=2.0,
                    help="dedupe nearby compacted/context_compacted representations (default: 2)")
    ap.add_argument("--compaction-direct-usage-seconds", type=float, default=1.0,
                    help="maximum near-marker window for direct compaction token usage attribution (default: 1)")
    ap.add_argument("--compaction-limit", type=int, default=30, help="maximum per-compaction rows to print")
    ap.add_argument("--prices", help="optional price JSON accepted by codex_quota_audit.py")
    ap.add_argument("--stage-limit", type=int, default=30, help="maximum active-window rows to print")
    ap.add_argument("--cycle-limit", type=int, default=20, help="maximum cycle rows to print")
    ap.add_argument("--agent-limit", type=int, default=20, help="maximum agent rows per detailed table")
    ap.add_argument("--schema-limit", type=int, default=30, help="maximum action-schema rows to print")
    ap.add_argument("--no-compaction-audit", action="store_true", help="skip explicit compaction/recovery analysis")
    ap.add_argument("--no-action-schema-audit", action="store_true", help="skip the structural action schema audit")
    ap.add_argument("--export-json", metavar="PATH", help="write privacy-safe machine-readable profile")
    ap.add_argument("--review-pauses", metavar="REPORT.json",
                    help="interactively review pauses using a saved v6.1+ report; no log rescan")
    ap.add_argument("--pause-store", metavar="DIRECTORY",
                    help="local pause annotation directory (default: XDG state directory/codex-quota-audit/pauses)")
    ap.add_argument("--quiet-gap-minutes", type=float, default=60,
                    help="suggest call-free intervals at least this long, without classifying them as pauses (default: 60)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0
    if args.review_pauses:
        if args.family or args.after or args.before or args.orchestrator:
            ap.error("--review-pauses uses the saved report's scope; do not combine it with session/window selection")
        try:
            return pauses.review_report(args.review_pauses, args.pause_store or pauses.default_store(), args.export_json)
        except (OSError, ValueError) as exc:
            ap.error(str(exc))
        except (EOFError, KeyboardInterrupt):
            print("\nPause review cancelled; unsaved decisions were not written.", file=sys.stderr)
            return 130
    if not args.family:
        ap.error("--family ID or --session SESSION_ID is required")

    roles = tuple(dict.fromkeys(r.strip().lower() for r in args.roles if r.strip()))
    stage_roles = set(r.strip().lower() for r in args.stage_roles if r.strip()) if args.stage_roles else None
    args.stage_roles = sorted(stage_roles) if stage_roles is not None else None
    if args.cycle_roles:
        args.cycle_roles = tuple(r.strip().lower() for r in args.cycle_roles)
    elif args.workflow_profile == "staged":
        args.cycle_roles = ("implementer", "validator")
    try:
        if not math.isfinite(args.quiet_gap_minutes) or args.quiet_gap_minutes <= 0:
            raise ValueError("--quiet-gap-minutes must be a positive finite number")
        analysis_after = parse_aware_iso8601(args.after, "--after")
        analysis_before = parse_aware_iso8601(args.before, "--before")
        if analysis_after is not None and analysis_before is not None and analysis_before <= analysis_after:
            raise ValueError("--before must be later than --after")
        if not (0 < args.compaction_refill_fraction <= 1):
            raise ValueError("--compaction-refill-fraction must be > 0 and <= 1")
        if args.compaction_resource_lookback_minutes < 0:
            raise ValueError("--compaction-resource-lookback-minutes must be >= 0")
        if args.compaction_dedupe_seconds < 0 or args.compaction_direct_usage_seconds < 0:
            raise ValueError("compaction timing windows must be >= 0")
        successor_map = parse_successor_map(args.successor, set(roles), args.workflow_profile)
        if stage_roles is not None and not stage_roles <= set(roles) | {"unknown", "guardian/auto-review"}:
            raise ValueError("--stage-roles must be configured with --roles (or use unknown/guardian/auto-review)")
        if args.cycle_roles and (len(set(args.cycle_roles)) != 2 or not set(args.cycle_roles) <= set(roles)):
            raise ValueError("--cycle-roles requires two distinct configured roles; use --roles to recognize custom names")
        if any(role not in roles for pair in successor_map.items() for role in pair):
            raise ValueError("--successor roles must be configured with --roles")
    except ValueError as exc:
        ap.error(str(exc))
    home = os.path.abspath(os.path.expanduser(args.home))
    prices = audit.load_prices(args.prices)
    args._prices = prices
    paths = finder.file_paths(home)

    print(f"Codex workflow cost profiler v{__version__}")
    print("=================================")
    print("Source: ~/.codex" if home == os.path.abspath(os.path.expanduser("~/.codex")) else f"Source: {home}")
    print(f"Files: {len(paths):,}")
    print(f"Roles: {', '.join(roles)}")
    print(f"Core activity: all roles; workflow interpretation: {args.workflow_profile}")
    print("Rebuilding privacy-safe family graph...\n")

    sessions: Dict[str, finder.Session] = {}
    for idx, path in enumerate(paths, 1):
        session = finder.scan_session(path, roles)
        key = session.session_key
        if key in sessions:
            key = finder.short_key("S", path)
            session.session_key = key
        sessions[key] = session
        if idx % 500 == 0:
            print(f"scanned {idx:,}/{len(paths):,} files...", flush=True)

    edges = finder.build_edges(sessions)
    families = finder.build_families(sessions, edges)
    family = lifecycle.resolve_family(args.family, families, sessions)
    if family is None:
        print("Could not uniquely resolve the supplied family/session selector against the current log graph.", file=sys.stderr)
        return 2

    print(f"\nParsing {len(family.members)} rollout files for {family.family_key}...")
    parsed: Dict[str, lifecycle.ParsedSession] = {}
    for key in family.members:
        parsed[key] = lifecycle.parse_family_session(sessions[key].path, key, roles)
    lifecycle.match_actions(
        family, sessions, parsed,
        spawn_window_minutes=args.spawn_window_minutes,
        tight_spawn_seconds=args.tight_spawn_seconds,
    )
    try:
        assignment_map = attribution.load_mapping(args.assignment_map, family, roles)
    except ValueError as exc:
        ap.error(str(exc))
    source_identities = attribution.resolve(family, sessions, parsed, roles, assignment_map)
    # Use resolved roles in all report views, retaining evidence confidence in
    # nested_attribution. Never alter the source logs or their inferred metadata.
    sessions = dict(sessions)
    for key, identity in source_identities.items():
        evidence = replace(sessions[key].roles, self_role=Counter())
        if identity["role"] != "unknown":
            evidence.self_role[identity["role"]] = 2
        sessions[key] = replace(sessions[key], roles=evidence)
    source_family = family
    parsed_full = parsed
    analysis_meta = classify_sessions_for_window(source_family, sessions, parsed_full, analysis_after, analysis_before)
    try:
        analysis_root, selection_method, selection_confidence, candidates = select_analysis_root(
            source_family, sessions, parsed_full,
            analysis_after, analysis_before, args.orchestrator,
        )
    except ValueError as exc:
        ap.error(str(exc))
    analysis_meta.root_selection_method = selection_method
    analysis_meta.root_selection_confidence = selection_confidence
    analysis_meta.orchestrator_candidates = candidates
    if analysis_root is None:
        print("\nCould not identify an unambiguous structural analysis root with activity in this window.", file=sys.stderr)
        if candidates:
            print("Top post-boundary candidates (privacy-safe session IDs):", file=sys.stderr)
            for row in candidates[:5]:
                print(
                    f"  {row['session_key']} activity_events={row['activity_events']} "
                    f"first_window={row['first_window_activity'].isoformat()}",
                    file=sys.stderr,
                )
        print("Use --analysis-root ID only after reviewing the candidate evidence.", file=sys.stderr)
        return 3

    family = build_analysis_family(source_family, sessions, parsed_full, analysis_meta, analysis_root)
    parsed = filter_parsed_for_window(
        family, parsed_full, analysis_meta.analysis_start, analysis_meta.analysis_end
    )
    identities = {k: dict(source_identities[k], issues=list(source_identities[k]["issues"])) for k in family.members}
    excluded_replay = Counter()
    for key, identity in identities.items():
        if key == family.root or identity["parent"] not in identities:
            identity["parent"] = None
            identity["parent_source"] = None
        activation = identity["activation"]
        if activation is not None:
            src = parsed[key]
            kept_usage = [r for r in src.usage if r.ts >= activation]
            kept_actions = [a for a in src.actions if a.ts >= activation]
            excluded_replay["requests"] += len(src.usage) - len(kept_usage)
            excluded_replay["total_tokens"] += sum(r.total_tokens for r in src.usage if r.ts < activation)
            excluded_replay["actions"] += len(src.actions) - len(kept_actions)
            src.usage, src.actions = kept_usage, kept_actions
    labels = lifecycle.assign_agent_labels(family, sessions, parsed)
    analysis_meta.carry_in_totals = carry_in_activity(analysis_meta, parsed_full, prices)

    nested = attribution.report(family, parsed, identities, labels, lambda rs: token_summary(rs, prices))
    nested["pre_activation_records_excluded"] = dict(excluded_replay)

    windows, excluded_windows = build_active_windows(
        family, sessions, parsed, labels, None, args.active_tail_seconds,
        analysis_meta.analysis_start, analysis_meta.analysis_end,
    )
    apply_assignment_windows(windows, identities, source_family.family_key)
    chunks = chunk_rows(family, parsed, windows, prices)

    role_totals = role_costs(family, sessions, parsed, prices)
    root_states = root_active_state_costs(family, parsed, windows, prices)
    root_child_counts = root_child_count_costs(family, parsed, windows, prices)
    concurrency = concurrency_metrics(windows, analysis_meta.analysis_start, analysis_meta.analysis_end)
    overlap_agents = agent_overlap_rows(windows)
    overlap_pairs = pairwise_overlap_rows(windows)
    lingering = build_lingering_exposures(family, parsed, windows, prices, successor_map)
    role_target_map = build_exact_role_target_map(
        family, sessions, roles, analysis_meta.analysis_start, analysis_meta.analysis_end
    )
    role_target_counts, role_target_costs, role_target_unmatched = role_targeted_root_send_costs(
        family, parsed, role_target_map, prices, args.action_inference_window_seconds
    )
    bursts = build_all_bursts(family, sessions, parsed, labels, args.burst_gap_seconds, prices)
    cycles = build_cycles(family, sessions, parsed, windows, prices, args.cycle_handoff_seconds, args.cycle_roles)
    workflow_analysis = {
        "profile": args.workflow_profile,
        "activity_scope": "all descendants with trusted spawn/lifetime evidence, regardless of role",
        "lifetime_note": "Observed lifetime overlap is not continuous execution, cost causation or duplicate work.",
        "sessions_without_lifetime_windows": [labels[k] for k in excluded_windows],
        "lifetime_windows_status": ("partial" if windows and excluded_windows else "available" if windows
                                    else "unavailable" if excluded_windows else "no_observed_descendants"),
        "cycle_roles": args.cycle_roles,
        "cycles_status": "not_configured" if not args.cycle_roles else "available" if cycles else "unavailable",
        "cycles_reason": ("No stage sequence assumed; configure --cycle-roles or the staged profile."
                          if not args.cycle_roles else "Observed transitions, not proof of task completion or review."
                          if cycles else "No supported consecutive transition for the configured role pair."),
        "role_filtered_activity": None,
    }
    if stage_roles is not None:
        selected_windows = [w for w in windows if w.role in stage_roles]
        workflow_analysis["role_filtered_activity"] = {
            "roles": sorted(stage_roles), "agents": sorted({w.target_label for w in selected_windows}),
            "concurrency": concurrency_metrics(selected_windows, analysis_meta.analysis_start, analysis_meta.analysis_end),
            "root_cost_by_concurrent_descendant_count": {
                k: totals_dict(v) for k, v in root_child_count_costs(family, parsed, selected_windows, prices).items()},
        }
    large_by_role, large_root_state = large_small_breakdown(
        family, sessions, parsed, windows, prices,
        args.large_context_input_tokens, args.small_output_tokens,
    )
    context_rows = session_context_stats(
        family, sessions, parsed, labels, prices,
        args.large_context_input_tokens, args.small_output_tokens,
    )
    compaction = None
    if not args.no_compaction_audit:
        print("Building context compaction audit...", flush=True)
        compaction = build_compaction_audit(
            family, sessions, parsed, labels, prices,
            analysis_meta.analysis_start, analysis_meta.analysis_end,
            dedupe_seconds=args.compaction_dedupe_seconds,
            direct_usage_seconds=args.compaction_direct_usage_seconds,
            refill_fraction=args.compaction_refill_fraction,
            resource_lookback_minutes=args.compaction_resource_lookback_minutes,
            heuristic_input_threshold=args.large_context_input_tokens,
        )
    comparison = comparison_metrics(
        family, role_totals, root_child_counts, concurrency, context_rows,
        large_by_role, lingering, windows, chunks, compaction,
    )
    schema = None
    if not args.no_action_schema_audit:
        print("Building privacy-safe action schema audit...", flush=True)
        schema = build_schema_audit(
            family, sessions, parsed, roles,
            analysis_meta.analysis_start, analysis_meta.analysis_end,
        )

    try:
        pause_entries, _ = pauses.load_annotations(args.pause_store or pauses.default_store(), family.root)
        pause_evidence = pauses.make_evidence(
            family.root, analysis_meta.analysis_start, analysis_meta.analysis_end,
            (r for key in family.members for r in parsed[key].usage),
        )
        pause_analysis = pauses.analyze(pause_evidence, pause_entries, args.quiet_gap_minutes)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))

    print_report(
        family, sessions, parsed, labels, windows, role_totals, root_states,
        bursts, cycles, context_rows, large_by_role, large_root_state,
        root_child_counts, concurrency, overlap_agents, overlap_pairs, lingering, successor_map,
        role_target_counts, role_target_costs, role_target_unmatched, schema,
        analysis_meta, chunks, comparison, compaction, args, workflow_analysis,
    )
    print_nested_report(nested)
    if compaction is not None:
        print("\nTool classification coverage (observed calls, not cost or waste):")
        for agent, row in compaction.tool_activity.items():
            print(f"  {agent}: {row['observed_events']} events, {row['unclassified_or_opaque']} unclassified/opaque; parse errors={row['parse_errors']}; {json.dumps(row['categories'], sort_keys=True)}")

    if args.export_json:
        export_json(
            args.export_json, family, sessions, parsed, labels, windows, role_totals,
            root_states, bursts, cycles, context_rows, large_by_role,
            large_root_state, root_child_counts, concurrency, overlap_agents,
            overlap_pairs, lingering, successor_map, role_target_counts, role_target_costs,
            role_target_unmatched, schema, analysis_meta, chunks, comparison, compaction, nested, workflow_analysis, pause_analysis,
        )
        print(f"\nWrote privacy-safe workflow cost profile: {args.export_json}")
    pauses.print_summary(pause_analysis, args.export_json, args.pause_store)

    print("\nInterpretation")
    print("--------------")
    print("Active-state labels come from trusted spawn matches, strong role metadata, and observed child lifetimes.")
    print("Exact role-targeted SEND labels are trusted at the role level; nearby inference pairing is observational.")
    print("Supervision ratios are emitted only for isolated sequential cycles; overlap/non-isolated ratios are suppressed.")
    print("Lifecycle overlap is classified by explicit chunk identity when available; unknown chunk identity stays unclassified.")
    print("Carry-in sessions are reported separately and excluded from primary window totals by default.")
    print("Explicit compaction events are counted directly; direct token cost is reported only when a nearby raw usage sample can be conservatively matched.")
    print("Post-compaction recovery/refill work is observed workload after compaction, not automatically causal overhead.")
    print("Repeated resource-read counts use one-way hashes locally; resource identities are never printed or exported.")
    print("Inference-burst re-entry is observational: a later burst is not automatically a follow-up.")
    print("Schema bridge candidates are diagnostics only and are not used for session-recipient attribution yet.")
    print("API$eq uses public list prices as a normalization ruler, not subscription billing/internal cost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
