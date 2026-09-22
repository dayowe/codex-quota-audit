#!/usr/bin/env python3
"""
Codex workflow candidate finder v2

Find recent, clean multi-agent workflow families in local Codex rollout logs.

This helper is intentionally read-only and privacy-conscious:
- scans ~/.codex/sessions and ~/.codex/archived_sessions
- reconstructs parent -> subagent relationships from session/thread/rollout IDs
- fingerprints linkage IDs before storing or printing them
- distinguishes stronger role metadata from noisy stdout/prompt mentions
- estimates token scale from token_count records
- ranks complete, manageable workflow families for later profiling
- never prints prompts, model responses, source code, tool stdout, or raw IDs
- makes no network requests

The goal is sample selection, not final cost attribution. Once a representative
workflow family is found, a separate structural extractor can inspect only that
family in more detail.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "2.3"

DEFAULT_ROLES = ("coordinator", "orchestrator", "planner", "implementer", "validator")

# Values under these paths are too likely to be prose/tool output to be useful
# as identity evidence. We do not retain or print the values.
NOISY_PATH_TERMS = {
    "stdout", "stderr", "aggregated_output", "formatted_output",
    "last_agent_message", "content", "text", "message", "messages",
    "prompt", "response", "output", "body", "instructions",
}

# Linkage discovery mirrors the privacy-safe approach used by codex_quota_audit.
LINK_DOMAIN = ("session", "thread", "conversation", "rollout", "parent", "subagent", "agent")
UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)

ACTION_PATTERNS = {
    "spawn": ("spawn_agent", "spawn_subagent", "create_agent", "create_subagent"),
    "send": ("send_input", "send_message", "message_agent", "follow_up", "followup"),
    "resume": ("resume_agent", "resume_subagent"),
    "wait": ("wait_agent", "wait_for_agent", "poll_agent", "wait"),
    "close": ("close_agent", "terminate_agent", "stop_agent"),
    "interrupt": ("interrupt_agent",),
}

SELF_ROLE_KEYS = {"role", "agent_role", "agent_type", "role_name"}
ROUTING_ROLE_KEYS = {"recipient", "agent_path", "subagent", "subagents", "agent", "agents"}


def parse_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fp(value: object) -> Optional[str]:
    """One-way fingerprint a compact ID-like scalar."""
    if value is None:
        return None
    if not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if len(text) < 6 or len(text) > 256:
        return None
    if "/" in text or "\\" in text or "\n" in text or "\r" in text:
        return None
    if len(text.split()) > 2:
        return None
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24]


def rollout_fp(path: str) -> Optional[str]:
    m = UUID_RE.search(os.path.basename(path))
    return fp(m.group(0)) if m else None


def short_key(prefix: str, seed: str) -> str:
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8', 'ignore')).hexdigest()[:10]}"


def nested_get(obj: object, path: Sequence[str]) -> object:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def scalar(value: object) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def source_kind_from_payload(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    src = payload.get("source")
    if isinstance(src, str):
        return src.strip().lower() or None
    if isinstance(src, dict):
        if "subagent" in src:
            return "subagent"
        if "guardian" in src:
            return "guardian"
        kind = src.get("type") or src.get("kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip().lower()
    return None


def model_from_payload(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("model"),
        nested_get(payload, ("state", "model")),
        nested_get(payload, ("thread_settings", "model")),
        nested_get(payload, ("collaboration_mode", "settings", "model")),
        nested_get(payload, ("state", "collaboration_mode", "model")),
    ]
    for value in candidates:
        s = scalar(value)
        if s:
            return s
    return None


def effort_from_payload(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("effort"),
        payload.get("reasoning_effort"),
        nested_get(payload, ("thread_settings", "reasoning_effort")),
        nested_get(payload, ("collaboration_mode", "settings", "reasoning_effort")),
        nested_get(payload, ("thread_settings", "collaboration_mode", "settings", "reasoning_effort")),
    ]
    for value in candidates:
        s = scalar(value)
        if s:
            return s.lower()
    return None


@dataclass
class LinkInfo:
    own_ids: set[str] = field(default_factory=set)
    parent_ids: set[str] = field(default_factory=set)
    schema_paths: Counter = field(default_factory=Counter)


def collect_link_ids(payload: object, info: LinkInfo) -> None:
    """Collect privacy-safe linkage fingerprints from structural metadata."""

    def walk(obj: object, path: Tuple[str, ...], depth: int) -> None:
        if depth > 8:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, path + (str(k).lower(),), depth + 1)
            return
        if isinstance(obj, list):
            for v in obj[:128]:
                walk(v, path + ("[]",), depth + 1)
            return
        if not path:
            return

        terminal = path[-1]
        path_text = ".".join(path)
        idish = (
            terminal.endswith("_id")
            or terminal in {"session", "thread", "conversation", "rollout"}
            or (terminal == "id" and any(word in path_text for word in LINK_DOMAIN))
        )
        if not idish or not any(word in path_text for word in LINK_DOMAIN):
            return

        ident = fp(obj)
        if not ident:
            return

        schema = ".".join(path[-6:])
        info.schema_paths[schema] += 1
        if "parent" in path_text or "source.subagent" in path_text:
            info.parent_ids.add(ident)
        else:
            info.own_ids.add(ident)

    walk(payload, tuple(), 0)


def is_noisy_path(path: Tuple[str, ...]) -> bool:
    return any(part.lower() in NOISY_PATH_TERMS for part in path)


def role_match(value: str, roles: Sequence[str]) -> List[str]:
    lower = value.lower()
    found = []
    for role in roles:
        if re.search(rf"(?<![a-z0-9]){re.escape(role)}(?![a-z0-9])", lower):
            found.append(role)
    return found


@dataclass
class RoleEvidence:
    self_role: Counter = field(default_factory=Counter)
    explicit_self_role: Counter = field(default_factory=Counter)
    routing_role: Counter = field(default_factory=Counter)
    weak_role: Counter = field(default_factory=Counter)
    self_paths: Counter = field(default_factory=Counter)
    routing_paths: Counter = field(default_factory=Counter)


def collect_role_evidence(payload: object, roles: Sequence[str], source_kind: str,
                          evidence: RoleEvidence, event_kind: Optional[str] = None) -> None:
    """Find role labels without retaining or printing surrounding text.

    Self evidence comes only from this session's session_meta fields, at the
    top level or in its source.subagent/thread_spawn metadata. Exact role fields
    are explicit declarations; a task path's leaf is weaker naming evidence.
    Ancestors, recipients and item.agent_path in messages are not self evidence.

    Weak evidence is counted only for diagnostics and never used to classify a
    session or decide that a workflow is complete.
    """

    def walk(obj: object, path: Tuple[str, ...], depth: int) -> None:
        if depth > 10:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, path + (str(k).lower(),), depth + 1)
            return
        if isinstance(obj, list):
            for v in obj[:128]:
                walk(v, path + ("[]",), depth + 1)
            return
        if not isinstance(obj, str):
            return

        terminal = path[-1] if path else ""
        # A canonical task path names ancestors too. Only its final component
        # can describe the current agent; do not classify by an ancestor role.
        matched = role_match(obj.rsplit("/", 1)[-1] if terminal == "agent_path" else obj, roles)
        if not matched:
            return

        path_text = ".".join(path)
        noisy = is_noisy_path(path)
        own_metadata = event_kind == "session_meta" and path[:-1] in {
            (), ("source", "subagent"), ("source", "subagent", "thread_spawn"),
        }
        explicit_role = own_metadata and terminal in SELF_ROLE_KEYS and obj.lower() in roles
        own_path = own_metadata and terminal == "agent_path" and source_kind == "subagent"

        for role in matched:
            if noisy:
                evidence.weak_role[role] += 1
                continue

            if explicit_role or own_path:
                evidence.self_role[role] += 1
                evidence.self_paths[(role, ".".join(path[-6:]))] += 1
                if explicit_role:
                    evidence.explicit_self_role[role] += 1
                continue

            routing = (
                terminal in ROUTING_ROLE_KEYS
                or "state.environments.subagents" in path_text
                or ".recipient" in path_text
                or ".agent_path" in path_text
            )
            if routing:
                evidence.routing_role[role] += 1
                evidence.routing_paths[(role, ".".join(path[-6:]))] += 1
            else:
                evidence.weak_role[role] += 1

    walk(payload, tuple(), 0)


def collect_actions(payload: object, counter: Counter) -> None:
    """Count structural agent-lifecycle action names without reading prose."""

    def classify(value: str) -> Optional[str]:
        lower = value.lower()
        for kind, patterns in ACTION_PATTERNS.items():
            if any(p in lower for p in patterns):
                return kind
        return None

    # Limit inspection to structural name/type-ish fields. Never inspect content.
    def walk(obj: object, path: Tuple[str, ...], depth: int) -> None:
        if depth > 7:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k).lower()
                p2 = path + (key,)
                if key in {"name", "tool_name", "type", "kind", "recipient"} and isinstance(v, str):
                    action = classify(v)
                    if action:
                        counter[action] += 1
                if key not in NOISY_PATH_TERMS:
                    walk(v, p2, depth + 1)
            return
        if isinstance(obj, list):
            for v in obj[:64]:
                walk(v, path + ("[]",), depth + 1)

    walk(payload, tuple(), 0)


def usage_from_payload(payload: object) -> Optional[Tuple[int, int, int, Optional[Tuple[int, int, int]]]]:
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    if ptype not in ("token_count", "token_usage_record"):
        return None
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    last = info.get("last_token_usage") or payload.get("last_token_usage")
    total = info.get("total_token_usage") or payload.get("total_token_usage")
    if not isinstance(last, dict):
        return None

    inp = int(last.get("input_tokens", 0) or 0)
    cached = int(last.get("cached_input_tokens", 0) or 0)
    out = int(last.get("output_tokens", 0) or 0)

    cumulative = None
    if isinstance(total, dict):
        cumulative = tuple(int(total.get(k, 0) or 0) for k in
                           ("input_tokens", "cached_input_tokens", "output_tokens"))
    return inp, cached, out, cumulative


@dataclass
class Session:
    path: str
    session_key: str
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    source_kind: str = "unknown"
    source_updates: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)
    efforts: Counter = field(default_factory=Counter)
    links: LinkInfo = field(default_factory=LinkInfo)
    roles: RoleEvidence = field(default_factory=RoleEvidence)
    actions: Counter = field(default_factory=Counter)
    usage_records: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    duplicate_usage_records: int = 0
    parse_errors: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_tokens(self) -> int:
        return max(self.input_tokens - self.cached_tokens, 0)

    @property
    def cache_share(self) -> float:
        return self.cached_tokens / self.input_tokens if self.input_tokens else float("nan")

    @property
    def dominant_model(self) -> str:
        return self.models.most_common(1)[0][0] if self.models else "unknown"

    @property
    def dominant_effort(self) -> str:
        return self.efforts.most_common(1)[0][0] if self.efforts else "unknown"

    @property
    def inferred_role(self) -> Tuple[str, str]:
        """Return (role, confidence) from strong self-role evidence only."""
        if not self.roles.self_role:
            return "unknown", "none"
        ranked = self.roles.self_role.most_common()
        role, n = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        if n >= max(2, second * 2):
            return role, "high"
        if n > second:
            return role, "medium"
        return "unknown", "ambiguous"


@dataclass
class Edge:
    parent: str
    child: str
    confidence: str
    method: str
    shared_ids: int = 0
    seconds_from_parent_activity: float = float("nan")


@dataclass
class Family:
    members: List[str]
    root: str
    edges: List[Edge]
    family_key: str
    score: float = 0.0
    sample_quality: str = ""

    def first_ts(self, sessions: Dict[str, Session]) -> Optional[datetime]:
        vals = [sessions[k].first_ts for k in self.members if sessions[k].first_ts]
        return min(vals) if vals else None

    def last_ts(self, sessions: Dict[str, Session]) -> Optional[datetime]:
        vals = [sessions[k].last_ts for k in self.members if sessions[k].last_ts]
        return max(vals) if vals else None


def file_paths(home: str) -> List[str]:
    return sorted(set(
        glob.glob(os.path.join(home, "sessions", "**", "*.jsonl"), recursive=True)
        + glob.glob(os.path.join(home, "archived_sessions", "*.jsonl"))
    ))


def scan_session(path: str, roles: Sequence[str]) -> Session:
    path_seed = rollout_fp(path) or hashlib.sha256(path.encode()).hexdigest()[:24]
    s = Session(path=path, session_key=short_key("S", path_seed))
    rf = rollout_fp(path)
    if rf:
        s.links.own_ids.add(rf)

    model: Optional[str] = None
    effort: Optional[str] = None
    source_kind = "unknown"
    prev_total: Optional[Tuple[int, int, int]] = None

    try:
        fh = open(path, "rb")
    except OSError:
        return s

    with fh:
        for line_no, raw in enumerate(fh, 1):
            # Parse only records likely to contain metadata, role labels, or usage.
            low = raw.lower()
            role_hint = any(role.encode() in low for role in roles)
            metadata_hint = (
                line_no <= 80
                or role_hint
                or b'"token_count"' in raw
                or b'"token_usage_record"' in raw
                or b'"source"' in raw
                or b'"parent' in raw
                or b'"session_id"' in raw
                or b'"thread_id"' in raw
                or b'"conversation_id"' in raw
                or b'"rollout_id"' in raw
                or b'"subagent"' in raw
                or b'"agent_path"' in raw
                or b'"recipient"' in raw
                or b'"environments"' in raw
                or b'"model"' in raw
                or b'"effort"' in raw
                or b'"reasoning_effort"' in raw
            )
            if not metadata_hint:
                continue

            try:
                obj = json.loads(raw)
            except Exception:
                s.parse_errors += 1
                continue

            ts = parse_ts(obj.get("timestamp"))
            if ts is not None:
                if s.first_ts is None or ts < s.first_ts:
                    s.first_ts = ts
                if s.last_ts is None or ts > s.last_ts:
                    s.last_ts = ts

            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue

            new_source = source_kind_from_payload(payload)
            if new_source:
                source_kind = new_source
                s.source_updates[new_source] += 1
                s.source_kind = new_source

            new_model = model_from_payload(payload)
            if new_model:
                model = new_model
            new_effort = effort_from_payload(payload)
            if new_effort:
                effort = new_effort

            collect_link_ids(payload, s.links)
            if role_hint:
                collect_role_evidence(payload, roles, source_kind, s.roles, obj.get("type"))
            collect_actions(payload, s.actions)

            usage = usage_from_payload(payload)
            if usage is None:
                continue
            inp, cached, out, cumulative = usage
            if cumulative is not None and cumulative == prev_total:
                s.duplicate_usage_records += 1
                continue
            if cumulative is not None:
                prev_total = cumulative

            s.usage_records += 1
            s.input_tokens += inp
            s.cached_tokens += min(cached, inp)
            s.output_tokens += out
            if model:
                s.models[model] += 1
            if effort:
                s.efforts[effort] += 1

    return s


def session_time_distance(parent: Session, child: Session) -> float:
    """Seconds from child start to nearest point in parent's observed activity span."""
    if child.first_ts is None or parent.first_ts is None:
        return float("inf")
    c = child.first_ts
    p0 = parent.first_ts
    p1 = parent.last_ts or p0
    if p0 <= c <= p1:
        return 0.0
    return min(abs((c - p0).total_seconds()), abs((c - p1).total_seconds()))


def build_edges(sessions: Dict[str, Session], allow_shared: bool = True) -> List[Edge]:
    own_index: Dict[str, set[str]] = defaultdict(set)
    for key, s in sessions.items():
        for ident in s.links.own_ids:
            own_index[ident].add(key)

    edges: List[Edge] = []

    for child_key, child in sessions.items():
        if child.source_kind not in {"subagent", "guardian"} and not child.links.parent_ids:
            continue

        explicit_counts: Counter = Counter()
        for ident in child.links.parent_ids:
            for parent_key in own_index.get(ident, ()):
                if parent_key != child_key:
                    explicit_counts[parent_key] += 1

        if explicit_counts:
            ranked = sorted(
                explicit_counts,
                key=lambda p: (-explicit_counts[p], session_time_distance(sessions[p], child), p)
            )
            parent_key = ranked[0]
            ambiguous = (
                len(ranked) > 1
                and explicit_counts[ranked[0]] == explicit_counts[ranked[1]]
                and abs(session_time_distance(sessions[ranked[0]], child)
                        - session_time_distance(sessions[ranked[1]], child)) < 5
            )
            edges.append(Edge(
                parent=parent_key,
                child=child_key,
                confidence="medium" if ambiguous else "high",
                method="explicit-parent-id",
                shared_ids=explicit_counts[parent_key],
                seconds_from_parent_activity=session_time_distance(sessions[parent_key], child),
            ))
            continue

        if not allow_shared or child.source_kind not in {"subagent", "guardian"}:
            continue

        shared_counts: Counter = Counter()
        child_ids = child.links.own_ids
        if not child_ids:
            continue
        for ident in child_ids:
            for parent_key in own_index.get(ident, ()):
                if parent_key != child_key:
                    shared_counts[parent_key] += 1

        if shared_counts:
            ranked = sorted(
                shared_counts,
                key=lambda p: (-shared_counts[p], session_time_distance(sessions[p], child), p)
            )
            parent_key = ranked[0]
            dist = session_time_distance(sessions[parent_key], child)
            # Shared IDs are weaker than explicit parent IDs. Require temporal
            # plausibility so a reused thread ID does not create giant families.
            if dist <= 6 * 3600:
                edges.append(Edge(
                    parent=parent_key,
                    child=child_key,
                    confidence="medium",
                    method="shared-link-id",
                    shared_ids=shared_counts[parent_key],
                    seconds_from_parent_activity=dist,
                ))

    # Keep only one best parent edge per child.
    by_child: Dict[str, List[Edge]] = defaultdict(list)
    for edge in edges:
        by_child[edge.child].append(edge)

    final = []
    conf_rank = {"high": 0, "medium": 1, "low": 2}
    for child_key, candidates in by_child.items():
        candidates.sort(key=lambda e: (
            conf_rank.get(e.confidence, 9),
            -e.shared_ids,
            e.seconds_from_parent_activity,
            e.parent,
        ))
        final.append(candidates[0])
    return final


def connected_components(sessions: Dict[str, Session], edges: Sequence[Edge]) -> List[set[str]]:
    adj: Dict[str, set[str]] = defaultdict(set)
    for e in edges:
        adj[e.parent].add(e.child)
        adj[e.child].add(e.parent)

    comps = []
    seen = set()
    for start in sorted(sessions):
        if start in seen:
            continue
        stack = [start]
        comp = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            comp.add(cur)
            stack.extend(adj[cur] - seen)
        comps.append(comp)
    return comps


def choose_root(comp: set[str], sessions: Dict[str, Session], edges: Sequence[Edge]) -> str:
    incoming = {e.child for e in edges if e.child in comp and e.parent in comp}
    candidates = [k for k in comp if k not in incoming]
    if not candidates:
        candidates = list(comp)

    def rank(key: str) -> Tuple[int, float, str]:
        s = sessions[key]
        source_rank = 0 if s.source_kind not in {"subagent", "guardian"} else 1
        ts = s.first_ts.timestamp() if s.first_ts else float("inf")
        return (source_rank, ts, key)

    return sorted(candidates, key=rank)[0]


def family_role_summary(family: Family, sessions: Dict[str, Session]) -> Counter:
    out = Counter()
    for key in family.members:
        role, conf = sessions[key].inferred_role
        if role != "unknown":
            out[role] += 1

    # Routing to orchestrators does not establish the root's own role.
    if sessions[family.root].inferred_role[0] == "unknown":
        out["unknown-root"] += 1
    return out


def score_family(family: Family, sessions: Dict[str, Session]) -> Tuple[float, str]:
    """Rank inspectable samples by telemetry/link coverage, never job titles."""
    edges = family.edges
    high = sum(e.confidence == "high" for e in edges)
    medium = sum(e.confidence == "medium" for e in edges)
    children = [k for k in family.members if k != family.root]
    non_guardian_children = [k for k in children if sessions[k].dominant_model != "codex-auto-review"]

    first = family.first_ts(sessions)
    last = family.last_ts(sessions)
    hours = (last - first).total_seconds() / 3600 if first and last else float("inf")

    score = 0.0
    score += 50 * high / len(edges) if edges else 0
    score += 15 * medium / len(edges) if edges else 0
    score += min(len(non_guardian_children), 20) * 4
    measured = sum(sessions[k].tokens > 0 for k in family.members)
    score += 40 * measured / len(family.members)
    if 2 <= len(non_guardian_children) <= 30:
        score += 20
    if hours <= 48:
        score += 20
    elif hours <= 96:
        score += 10
    if high == len(edges) and edges:
        score += 20

    if not measured:
        quality = "no usage telemetry"
    elif len(family.members) == 1:
        quality = "standalone session"
    elif high == len(edges) and measured == len(family.members):
        quality = "linked, complete usage coverage"
    else:
        quality = "linked, limited evidence"

    return score, quality


def build_families(sessions: Dict[str, Session], edges: Sequence[Edge]) -> List[Family]:
    comps = connected_components(sessions, edges)
    families = []
    for comp in comps:
        comp_edges = [e for e in edges if e.parent in comp and e.child in comp]
        root = choose_root(comp, sessions, comp_edges)
        seed = "|".join(sorted(sessions[k].session_key for k in comp))
        family = Family(
            members=sorted(comp),
            root=root,
            edges=comp_edges,
            family_key=short_key("W", seed),
        )
        family.score, family.sample_quality = score_family(family, sessions)
        families.append(family)
    return families


def fmt_dt(dt: Optional[datetime]) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "?"


def fmt_duration(a: Optional[datetime], b: Optional[datetime]) -> str:
    if not a or not b:
        return "?"
    sec = max(0.0, (b - a).total_seconds())
    if sec < 3600:
        return f"{sec/60:.0f}m"
    if sec < 72 * 3600:
        return f"{sec/3600:.1f}h"
    return f"{sec/86400:.1f}d"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.1f}K"
    return str(n)


def fmt_cache(s: Session) -> str:
    return f"{100*s.cache_share:.0f}%" if math.isfinite(s.cache_share) else "-"


def action_text(c: Counter) -> str:
    order = ("spawn", "send", "resume", "wait", "close")
    vals = [f"{k}:{c[k]}" for k in order if c.get(k)]
    return ",".join(vals) if vals else "-"


def role_text(s: Session) -> str:
    role, conf = s.inferred_role
    if role != "unknown":
        return f"{role}({conf})"
    # For roots, routing evidence is still useful but explicitly marked.
    if s.roles.routing_role:
        r, n = s.roles.routing_role.most_common(1)[0]
        return f"{r}?(routing)"
    return "unknown"


def edge_for_child(family: Family) -> Dict[str, Edge]:
    return {e.child: e for e in family.edges}


def session_line(s: Session, relation: str = "") -> str:
    rel = f"{relation:<6} " if relation else ""
    return (
        f"{rel}{s.session_key:<13} role={role_text(s):<23} "
        f"source={s.source_kind:<9} model={s.dominant_model:<15} effort={s.dominant_effort:<7} "
        f"tokens={fmt_tokens(s.tokens):>7} cache={fmt_cache(s):>4} "
        f"actions={action_text(s.actions)}"
    )


def family_tokens(f: Family, sessions: Dict[str, Session]) -> int:
    return sum(sessions[k].tokens for k in f.members)


def family_roles(f: Family, sessions: Dict[str, Session]) -> str:
    c = family_role_summary(f, sessions)
    if not c:
        return "-"
    return ",".join(f"{k}:{v}" for k, v in sorted(c.items()))


def export_json(path: str, families: Sequence[Family], sessions: Dict[str, Session], top: int) -> None:
    payload = {
        "schema": "codex-workflow-candidates-v2",
        "version": __version__,
        "privacy": "No prompts/responses/raw linkage IDs are included.",
        "families": [],
    }
    for f in families[:top]:
        emap = edge_for_child(f)
        members = []
        for key in f.members:
            s = sessions[key]
            edge = emap.get(key)
            role, role_conf = s.inferred_role
            members.append({
                "session_key": s.session_key,
                "root": key == f.root,
                "source_kind": s.source_kind,
                "role": role,
                "role_confidence": role_conf,
                "dominant_model": s.dominant_model,
                "dominant_effort": s.dominant_effort,
                "first_ts": s.first_ts.isoformat() if s.first_ts else None,
                "last_ts": s.last_ts.isoformat() if s.last_ts else None,
                "tokens": s.tokens,
                "input_tokens": s.input_tokens,
                "cached_input_tokens": s.cached_tokens,
                "output_tokens": s.output_tokens,
                "actions": dict(s.actions),
                "parent_session_key": sessions[edge.parent].session_key if edge else None,
                "link_confidence": edge.confidence if edge else None,
                "link_method": edge.method if edge else None,
            })
        payload["families"].append({
            "family_key": f.family_key,
            "sample_quality": f.sample_quality,
            "score": round(f.score, 2),
            "first_ts": f.first_ts(sessions).isoformat() if f.first_ts(sessions) else None,
            "last_ts": f.last_ts(sessions).isoformat() if f.last_ts(sessions) else None,
            "roles": dict(family_role_summary(f, sessions)),
            "tokens": family_tokens(f, sessions),
            "sessions": members,
        })
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def self_test() -> None:
    # Test role matching and privacy-safe linkage graph with synthetic sessions.
    roles = DEFAULT_ROLES
    ev = RoleEvidence()
    collect_role_evidence(
        {"source": {"subagent": {"role": "implementer"}}},
        roles, "subagent", ev, "session_meta"
    )
    assert ev.self_role["implementer"] == 1

    root = Session("/tmp/root.jsonl", "S-root", source_kind="cli")
    impl = Session("/tmp/impl.jsonl", "S-impl", source_kind="subagent")
    val = Session("/tmp/val.jsonl", "S-val", source_kind="subagent")
    a, b, c = fp("root-thread-123"), fp("impl-thread-456"), fp("val-thread-789")
    assert a and b and c
    root.links.own_ids.add(a)
    impl.links.parent_ids.add(a)
    impl.links.own_ids.add(b)
    val.links.parent_ids.add(a)
    val.links.own_ids.add(c)
    impl.roles.self_role["implementer"] = 3
    val.roles.self_role["validator"] = 3

    ss = {x.session_key: x for x in (root, impl, val)}
    edges = build_edges(ss)
    assert len(edges) == 2
    fams = build_families(ss, edges)
    assert len(fams) == 1
    assert len(fams[0].members) == 3
    assert family_role_summary(fams[0], ss)["implementer"] == 1
    assert family_role_summary(fams[0], ss)["validator"] == 1
    print("self-test: OK")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Find recent Codex multi-agent workflow families using explicit "
            "parent/subagent linkage. No prompt/response text is printed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 find_workflow_candidates.py
  python3 find_workflow_candidates.py --top 15
  python3 find_workflow_candidates.py --recent-days 45
  python3 find_workflow_candidates.py --roles orchestrator implementer validator
  python3 find_workflow_candidates.py --export-json workflow_candidates.json
  python3 find_workflow_candidates.py --show-paths
  python3 find_workflow_candidates.py --self-test
""",
    )
    ap.add_argument("--home", default=os.path.expanduser("~/.codex"), help="Codex data directory")
    ap.add_argument("--roles", nargs="+", default=list(DEFAULT_ROLES),
                    help="role labels to recognize")
    ap.add_argument("--top", type=int, default=10,
                    help="number of workflow families to show (default: 10)")
    ap.add_argument("--recent-days", type=float, default=90,
                    help="prefer families ending within this many days of the newest log (default: 90)")
    ap.add_argument("--show-paths", action="store_true",
                    help="show ~/.codex-relative file paths; off by default for easier sharing")
    ap.add_argument("--show-link-schema", action="store_true",
                    help="show the most common linkage metadata paths")
    ap.add_argument("--no-shared-link-fallback", action="store_true",
                    help="use only explicit parent IDs, not shared-ID fallback links")
    ap.add_argument("--export-json", metavar="PATH",
                    help="write a privacy-safe machine-readable candidate manifest")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    roles = tuple(dict.fromkeys(r.strip().lower() for r in args.roles if r.strip()))
    home = os.path.abspath(os.path.expanduser(args.home))
    paths = file_paths(home)

    print(f"Codex workflow candidate finder v{__version__}")
    print("=" * 38)
    print("Source: ~/.codex" if home == os.path.abspath(os.path.expanduser("~/.codex")) else f"Source: {home}")
    print(f"Files: {len(paths):,}")
    print(f"Roles: {', '.join(roles)}")
    print("Linkage IDs are one-way hashed; prompts/responses/tool output are never printed.\n")

    sessions: Dict[str, Session] = {}
    link_schema = Counter()

    for idx, path in enumerate(paths, 1):
        s = scan_session(path, roles)
        # session_key collisions are extremely unlikely; preserve determinism if one occurs.
        key = s.session_key
        if key in sessions:
            key = short_key("S", path)
            s.session_key = key
        sessions[key] = s
        link_schema.update(s.links.schema_paths)
        if idx % 500 == 0:
            print(f"scanned {idx:,}/{len(paths):,} files...", flush=True)

    edges = build_edges(sessions, allow_shared=not args.no_shared_link_fallback)
    families = build_families(sessions, edges)

    explicit = sum(e.method == "explicit-parent-id" for e in edges)
    shared = sum(e.method == "shared-link-id" for e in edges)
    subagents = sum(s.source_kind == "subagent" for s in sessions.values())
    linked_children = len({e.child for e in edges})
    role_self = sum(bool(s.roles.self_role) for s in sessions.values())

    print("\nLinkage audit")
    print("-------------")
    print(f"subagent sessions discovered:       {subagents:,}")
    print(f"subagent/child sessions linked:     {linked_children:,}")
    print(f"explicit parent-ID links:           {explicit:,}")
    print(f"shared-ID fallback links:           {shared:,}")
    print(f"sessions with strong self-role data:{role_self:>9,}")
    print(f"connected workflow families:        {len(families):,}")

    if args.show_link_schema:
        print("\nLinkage schema discovery")
        print("------------------------")
        for path_name, n in link_schema.most_common(30):
            print(f"{n:>8,}  {path_name}")

    # Prefer recent families, but fall back to all if the recent window is sparse.
    newest = max((s.last_ts for s in sessions.values() if s.last_ts), default=None)
    cutoff = newest - timedelta(days=args.recent_days) if newest else None

    recent = [
        f for f in families
        if cutoff is None or (f.last_ts(sessions) is not None and f.last_ts(sessions) >= cutoff)
    ]
    pool = recent if recent else families

    # Favor inspectable linked samples, retaining standalone sessions as well.
    quality_rank = {
        "linked, complete usage coverage": 0,
        "linked, limited evidence": 1,
        "standalone session": 2,
        "no usage telemetry": 3,
    }
    pool.sort(key=lambda f: (
        quality_rank.get(f.sample_quality, 9),
        -(f.last_ts(sessions).timestamp() if f.last_ts(sessions) else 0),
        -f.score,
    ))

    print("\nBest recent workflow families")
    print("-----------------------------")
    print(
        "These are graph-linked families or standalone sessions, not time-gap clusters. "
        "Ranking uses linkage and usage coverage, not workflow role names."
    )

    shown = pool[: max(0, args.top)]
    if not shown:
        print("No workflow families or standalone sessions found.")
        print("Rerun with --show-link-schema and share the linkage audit.")
    else:
        for i, f in enumerate(shown, 1):
            first, last = f.first_ts(sessions), f.last_ts(sessions)
            high = sum(e.confidence == "high" for e in f.edges)
            medium = sum(e.confidence == "medium" for e in f.edges)
            print(
                f"\n{i:02d}. {f.family_key}  {f.sample_quality.upper()}"
            )
            print(
                f"    {fmt_dt(first)} -> {fmt_dt(last)}  "
                f"span={fmt_duration(first, last)}  sessions={len(f.members)}  "
                f"tokens≈{fmt_tokens(family_tokens(f, sessions))}"
            )
            print(
                f"    roles={family_roles(f, sessions)}  "
                f"links=high:{high}, medium:{medium}"
            )

            root = sessions[f.root]
            print("    " + session_line(root, "ROOT"))

            child_edges = edge_for_child(f)
            children = [k for k in f.members if k != f.root]
            children.sort(key=lambda k: (
                sessions[k].first_ts or datetime.max.replace(tzinfo=timezone.utc),
                k,
            ))
            for key in children[:20]:
                child = sessions[key]
                edge = child_edges.get(key)
                print("    " + session_line(child, "CHILD"))
                if edge:
                    print(
                        f"           parent={sessions[edge.parent].session_key} "
                        f"link={edge.method}/{edge.confidence} "
                        f"shared_ids={edge.shared_ids}"
                    )
                if args.show_paths:
                    try:
                        rel = os.path.relpath(child.path, home)
                        print(f"           path=~/.codex/{rel}")
                    except Exception:
                        pass
            if len(children) > 20:
                print(f"           ... {len(children)-20} more linked child sessions")

            if args.show_paths:
                try:
                    rel = os.path.relpath(root.path, home)
                    print(f"           root_path=~/.codex/{rel}")
                except Exception:
                    pass

    # Also show why sessions remain unlinked, useful if explicit structure is sparse.
    unlinked_subagents = [
        s for s in sessions.values()
        if s.source_kind == "subagent" and s.session_key not in {e.child for e in edges}
    ]
    unlinked_subagents.sort(
        key=lambda s: s.last_ts or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if unlinked_subagents:
        print("\nUnlinked subagent summary")
        print("-------------------------")
        print(f"unlinked subagent sessions: {len(unlinked_subagents):,}")
        print("Most recent examples (metadata only):")
        for s in unlinked_subagents[:5]:
            print("  " + session_line(s))

    if args.export_json:
        export_json(args.export_json, pool, sessions, max(args.top, 1))
        print(f"\nWrote privacy-safe candidate manifest: {args.export_json}")

    print("\nWhat to send back")
    print("-----------------")
    print(
        "Paste/upload this output (or the --export-json manifest). "
        "The W-xxxxxxxxxx family IDs are enough for us to choose one or two samples."
    )
    print(
        "Do not upload raw rollout files yet. After we choose a family, "
        "the next helper will extract only its structural lifecycle and token data."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
