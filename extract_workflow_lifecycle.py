#!/usr/bin/env python3
"""
Codex workflow lifecycle extractor v2.2.1

Companion to find_workflow_candidates.py.

Given a workflow family (W-...) or a stable session seed (S-...), this script
rescans only that family's rollout files and reconstructs a privacy-safe
structural timeline from actual agent lifecycle tool calls where possible.

It is designed to answer questions such as:
- Which spawned agents are implementers vs validators?
- How many follow-up messages does each agent receive?
- How much model work happens on the initial pass vs follow-up rounds?
- How much parent/orchestrator inference is associated with sends/spawns/waits?
- Which individual inference requests repeatedly reread very large contexts?

Privacy properties:
- read-only; no network requests
- never prints prompts, model responses, source code, tool stdout, or raw IDs
- hashes IDs before matching/printing
- JSON export contains structural metadata and aggregate token counts only

This is an experimental structural profiler. Tool-call schemas can evolve, so
trusted/diagnostic/unresolved coverage is printed explicitly instead of hiding
uncertainty. v2 deliberately refuses to use single-active-agent guesses for
cost attribution: low-confidence guesses remain diagnostic only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import find_workflow_candidates as finder
except ImportError as exc:  # pragma: no cover - user-facing path
    raise SystemExit(
        "extract_workflow_lifecycle.py must be in the same directory as "
        "find_workflow_candidates.py"
    ) from exc

__version__ = "2.2.1"

ACTION_NAMES = {
    "spawn": ("spawn_agent", "spawn_subagent", "create_agent", "create_subagent"),
    "send": ("send_input", "send_message", "message_agent", "follow_up", "followup", "followup_task"),
    "resume": ("resume_agent", "resume_subagent"),
    "wait": ("wait_agent", "wait_for_agent", "poll_agent", "wait"),
    "close": ("close_agent", "terminate_agent", "stop_agent"),
    "interrupt": ("interrupt_agent",),
}

NOISY_KEYS = {
    "message", "messages", "prompt", "prompts", "content", "text", "body",
    "instructions", "stdout", "stderr", "output_text", "response", "responses",
    "last_agent_message", "formatted_output", "aggregated_output",
}

ID_KEYS = {
    "id", "agent_id", "agent_ids", "subagent_id", "subagent_ids",
    "thread_id", "thread_ids", "session_id", "session_ids", "rollout_id",
    "rollout_ids", "recipient_id", "recipient_ids", "target_id", "target_ids",
    "conversation_id", "conversation_ids",
}

ROLE_KEYS = {
    "role", "agent_role", "agent_type", "role_name", "recipient", "agent_path",
    "subagent", "subagents",
}

UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
NAMED_ID_RE = re.compile(
    r"(?i)\b(?:agent|subagent|thread|session|rollout)[-_][a-z0-9_-]{6,128}\b"
)


def parse_ts(value: object) -> Optional[datetime]:
    return finder.parse_ts(value)


def hash_id(value: object) -> Optional[str]:
    return finder.fp(value)


def short_hash(value: str, prefix: str = "X") -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8', 'ignore')).hexdigest()[:10]}"


def classify_action(name: object) -> Optional[str]:
    if not isinstance(name, str):
        return None
    lower = name.strip().lower()
    if not lower:
        return None
    for kind, patterns in ACTION_NAMES.items():
        if lower in patterns or any(lower.endswith("." + p) for p in patterns):
            return kind
        if any(p == lower.replace("-", "_") for p in patterns):
            return kind
    return None


def parse_jsonish(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def compact_scalar(value: object) -> bool:
    if not isinstance(value, (str, int, float)):
        return False
    text = str(value).strip()
    return 5 <= len(text) <= 256 and "\n" not in text and "\r" not in text and len(text.split()) <= 3


def collect_structural_ids(obj: object, *, allow_text_regex: bool = False,
                           depth: int = 0) -> set[str]:
    """Collect hashed ID-like values without retaining surrounding text."""
    out: set[str] = set()
    if depth > 9:
        return out

    obj = parse_jsonish(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).lower()
            if k in NOISY_KEYS:
                # Tool outputs sometimes put a tiny JSON object in `output`. That
                # path is handled by parse_jsonish at the caller, not by scanning
                # arbitrary prose here.
                continue
            if k in ID_KEYS or k.endswith("_id") or k.endswith("_ids"):
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if compact_scalar(item):
                        h = hash_id(item)
                        if h:
                            out.add(h)
            out |= collect_structural_ids(value, allow_text_regex=allow_text_regex, depth=depth + 1)
        return out

    if isinstance(obj, list):
        for value in obj[:256]:
            out |= collect_structural_ids(value, allow_text_regex=allow_text_regex, depth=depth + 1)
        return out

    if allow_text_regex and isinstance(obj, str):
        # Only extract ID-shaped tokens. Never retain/print the text itself.
        for match in list(UUID_RE.findall(obj)) + list(NAMED_ID_RE.findall(obj)):
            h = hash_id(match)
            if h:
                out.add(h)
    return out


def looks_like_identifier(value: object) -> bool:
    if not compact_scalar(value):
        return False
    text = str(value).strip()
    if UUID_RE.fullmatch(text) or NAMED_ID_RE.fullmatch(text):
        return True
    # Opaque agent IDs are often long alphanumeric tokens with separators.
    return len(text) >= 16 and bool(re.fullmatch(r"[A-Za-z0-9_-]+", text)) and any(ch.isdigit() for ch in text)


def collect_action_target_ids(obj: object, depth: int = 0, path: Tuple[str, ...] = ()) -> set[str]:
    """Collect only target-like IDs from lifecycle tool arguments.

    This is intentionally stricter than generic linkage discovery: unrelated
    thread/session IDs in an argument object must not become trusted targets.
    """
    out: set[str] = set()
    if depth > 9:
        return out
    obj = parse_jsonish(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).lower()
            if k in NOISY_KEYS:
                continue
            p2 = path + (k,)
            targetish = (
                k in {"agent_id", "agent_ids", "subagent_id", "subagent_ids",
                      "recipient_id", "recipient_ids", "target_id", "target_ids"}
                or (k in {"id", "ids"} and any(term in ".".join(path) for term in
                                                  ("agent", "subagent", "recipient", "target")))
                or k in {"agent", "recipient", "target"}
            )
            if targetish:
                vals = value if isinstance(value, list) else [value]
                for item in vals:
                    if looks_like_identifier(item):
                        h = hash_id(item)
                        if h:
                            out.add(h)
            out |= collect_action_target_ids(value, depth + 1, p2)
        return out
    if isinstance(obj, list):
        for value in obj[:256]:
            out |= collect_action_target_ids(value, depth + 1, path + ("[]",))
    return out


def collect_role_hints(obj: object, roles: Sequence[str], depth: int = 0) -> Counter:
    out: Counter = Counter()
    if depth > 8:
        return out
    obj = parse_jsonish(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).lower()
            if k in NOISY_KEYS:
                continue
            if k in ROLE_KEYS and isinstance(value, str):
                low = value.lower()
                for role in roles:
                    if re.search(rf"(?<![a-z0-9]){re.escape(role)}(?![a-z0-9])", low):
                        out[role] += 1
            out.update(collect_role_hints(value, roles, depth + 1))
    elif isinstance(obj, list):
        for value in obj[:128]:
            out.update(collect_role_hints(value, roles, depth + 1))
    return out


@dataclass
class ActionEvent:
    ts: datetime
    caller_key: str
    kind: str
    name: str
    call_id: Optional[str]
    target_ids: set[str] = field(default_factory=set)
    result_ids: set[str] = field(default_factory=set)
    role_hints: Counter = field(default_factory=Counter)
    # Trusted target used for cost attribution. Only high/medium structural
    # evidence may populate this field.
    matched_session: Optional[str] = None
    # Low-confidence hint retained for diagnostics/timeline only.
    diagnostic_session: Optional[str] = None
    match_method: str = "unmatched"
    match_confidence: str = "none"
    # Compact spawn identity only; never retain assignment prompts or export labels.
    task_name: Optional[str] = None
    task_name_conflict: bool = False


@dataclass
class UsageRequest:
    ts: datetime
    session_key: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    model: str
    effort: str
    cumulative_total: Optional[int] = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cached_input_tokens, 0)

    @property
    def cache_share(self) -> float:
        return self.cached_input_tokens / self.input_tokens if self.input_tokens else float("nan")


@dataclass
class ParsedSession:
    actions: List[ActionEvent] = field(default_factory=list)
    usage: List[UsageRequest] = field(default_factory=list)
    action_outputs: Dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    parse_errors: int = 0
    duplicate_actions_removed: int = 0


def compact_task_name(value):
    if isinstance(value, str) and len(value) <= 512 and not any(c.isspace() for c in value):
        leaf = value.rsplit("/", 1)[-1]
        if leaf.startswith("si1_"):
            return leaf
        return value
    return None


@dataclass
class AgentPhase:
    session_key: str
    agent_label: str
    role: str
    phase: str
    request_count: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ParentDecision:
    request: UsageRequest
    action: ActionEvent
    target_label: str
    target_role: str
    delta_seconds: float


def action_node_candidates(payload: object) -> Iterable[dict]:
    """Yield dictionaries that look like actual lifecycle tool invocations."""
    seen: set[int] = set()

    def walk(obj: object, depth: int) -> Iterable[dict]:
        if depth > 10:
            return
        if isinstance(obj, dict):
            oid = id(obj)
            if oid in seen:
                return
            seen.add(oid)

            if str(obj.get("type", "")).endswith(("_output", "_result")):
                return

            names = []
            for key in ("name", "tool_name"):
                if isinstance(obj.get(key), str):
                    names.append(obj[key])
            fn = obj.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                names.append(fn["name"])
            if any(classify_action(name) for name in names):
                yield obj
                return  # avoid re-yielding nested arguments from the same call

            for key, value in obj.items():
                if str(key).lower() in NOISY_KEYS:
                    continue
                yield from walk(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj[:256]:
                yield from walk(value, depth + 1)

    yield from walk(payload, 0)


def dedupe_actions(actions: Sequence[ActionEvent]) -> Tuple[List[ActionEvent], int]:
    """Collapse duplicate representations of the same lifecycle action.

    Codex can serialize one tool action through multiple nested/state views. When
    a call ID exists it is the primary key. Otherwise we conservatively collapse
    same-caller, same-timestamp, same-kind records with the same structural IDs.
    """
    merged: Dict[Tuple[object, ...], ActionEvent] = {}
    removed = 0
    for action in actions:
        if action.call_id:
            key = (action.caller_key, action.kind, "call", action.call_id)
        else:
            key = (
                action.caller_key,
                action.kind,
                "time",
                action.ts.isoformat(),
                tuple(sorted(action.target_ids)),
                tuple(sorted(action.role_hints.items())),
            )
        prev = merged.get(key)
        if prev is None:
            merged[key] = action
            continue
        removed += 1
        prev.target_ids |= action.target_ids
        prev.result_ids |= action.result_ids
        prev.role_hints.update(action.role_hints)
        if prev.task_name and action.task_name and prev.task_name != action.task_name:
            prev.task_name_conflict = True
        prev.task_name = prev.task_name or action.task_name
        prev.task_name_conflict |= action.task_name_conflict
    out = sorted(merged.values(), key=lambda a: (a.ts, a.caller_key, a.kind, a.call_id or ""))
    return out, removed


def is_trusted_action(action: ActionEvent) -> bool:
    return action.matched_session is not None and action.match_confidence in {"high", "medium"}


def displayed_target(action: ActionEvent) -> Optional[str]:
    return action.matched_session or action.diagnostic_session


def attribution_class(action: ActionEvent) -> str:
    if is_trusted_action(action):
        return "trusted"
    if action.diagnostic_session:
        return "diagnostic"
    return "unresolved"


def extract_output_record(payload: object) -> Optional[Tuple[str, set[str]]]:
    if not isinstance(payload, dict):
        return None
    ptype = str(payload.get("type", "")).lower()
    call_raw = payload.get("call_id") or payload.get("tool_call_id") or payload.get("function_call_id")
    if call_raw is None:
        return None
    looks_output = (
        "output" in ptype or "result" in ptype
        or "output" in payload or "result" in payload
    )
    if not looks_output:
        return None
    call_id = hash_id(call_raw)
    if not call_id:
        return None
    value = payload.get("output", payload.get("result"))
    parsed = parse_jsonish(value)
    ids = collect_structural_ids(parsed, allow_text_regex=isinstance(parsed, str))
    return call_id, ids


def extract_usage(payload: object, ts: datetime, session_key: str,
                  current_model: str, current_effort: str) -> Optional[UsageRequest]:
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
    reasoning = int(last.get("reasoning_output_tokens", 0) or 0)
    cumulative_total = None
    if isinstance(total, dict):
        v = total.get("total_tokens")
        if isinstance(v, (int, float)):
            cumulative_total = int(v)
        else:
            cumulative_total = int(total.get("input_tokens", 0) or 0) + int(total.get("output_tokens", 0) or 0)

    return UsageRequest(
        ts=ts,
        session_key=session_key,
        input_tokens=inp,
        cached_input_tokens=min(cached, inp),
        output_tokens=out,
        reasoning_tokens=reasoning,
        model=current_model,
        effort=current_effort,
        cumulative_total=cumulative_total,
    )


def parse_family_session(path: str, session_key: str, roles: Sequence[str]) -> ParsedSession:
    out = ParsedSession()
    current_model = "unknown"
    current_effort = "unknown"
    prev_cumulative: Optional[int] = None
    result_tasks = defaultdict(set)

    try:
        fh = open(path, "rb")
    except OSError:
        return out

    with fh:
        for raw in fh:
            # Fast skip while preserving model/effort/usage/action/output records.
            low = raw.lower()
            if not (
                b'"token_count"' in raw or b'"token_usage_record"' in raw
                or b'"function_call"' in raw or b'"tool_call"' in raw
                or b'"spawn_agent"' in low or b'"send_input"' in low
                or b'wait_agent' in low or b'send_message' in low or b'followup_task' in low
                or b'interrupt_agent' in low or b'"wait"' in low or b'"resume_agent"' in low or b'"close_agent"' in low
                or b'"model"' in raw or b'"effort"' in raw or b'"reasoning_effort"' in raw
                or b'"call_id"' in raw or b'"tool_call_id"' in raw
            ):
                continue

            try:
                obj = json.loads(raw)
            except Exception:
                out.parse_errors += 1
                continue

            ts = parse_ts(obj.get("timestamp"))
            payload = obj.get("payload")
            if ts is None or not isinstance(payload, dict):
                continue

            model = finder.model_from_payload(payload)
            if model:
                current_model = model
            effort = finder.effort_from_payload(payload)
            if effort:
                current_effort = effort

            usage = extract_usage(payload, ts, session_key, current_model, current_effort)
            if usage is not None:
                if usage.cumulative_total is not None and usage.cumulative_total == prev_cumulative:
                    continue
                if usage.cumulative_total is not None:
                    prev_cumulative = usage.cumulative_total
                out.usage.append(usage)

            output_record = extract_output_record(payload)
            if output_record:
                call_id, ids = output_record
                out.action_outputs[call_id].update(ids)
                result = parse_jsonish(payload.get("output", payload.get("result")))
                if isinstance(result, dict):
                    task_name = compact_task_name(result.get("task_name"))
                    if task_name:
                        result_tasks[call_id].add(task_name)

            for node in action_node_candidates(payload):
                names = []
                if isinstance(node.get("name"), str):
                    names.append(node["name"])
                if isinstance(node.get("tool_name"), str):
                    names.append(node["tool_name"])
                fn = node.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    names.append(fn["name"])
                name = next((n for n in names if classify_action(n)), None)
                kind = classify_action(name) if name else None
                if not kind:
                    continue

                args = node.get("arguments")
                if args is None:
                    args = node.get("args")
                if args is None and isinstance(fn, dict):
                    args = fn.get("arguments")
                args = parse_jsonish(args)

                task_name = compact_task_name(args.get("task_name")) if isinstance(args, dict) and kind == "spawn" else None

                call_raw = (
                    node.get("call_id") or node.get("tool_call_id")
                    or node.get("function_call_id") or node.get("id")
                )
                call_id = hash_id(call_raw) if call_raw is not None else None

                out.actions.append(ActionEvent(
                    ts=ts,
                    caller_key=session_key,
                    kind=kind,
                    name=str(name),
                    call_id=call_id,
                    target_ids=collect_action_target_ids(args),
                    role_hints=collect_role_hints(args, roles),
                    task_name=task_name,
                ))

    # Attach any separately logged tool outputs to their calls.
    for action in out.actions:
        if action.call_id and action.call_id in out.action_outputs:
            action.result_ids |= out.action_outputs[action.call_id]
        if action.kind == "spawn" and action.call_id in result_tasks:
            names = result_tasks[action.call_id] | ({action.task_name} if action.task_name else set())
            action.task_name_conflict = len(names) > 1
            if len(names) == 1:
                action.task_name = next(iter(names))

    out.actions, out.duplicate_actions_removed = dedupe_actions(out.actions)
    out.usage.sort(key=lambda u: u.ts)
    return out


def role_for_session(session: finder.Session) -> str:
    if session.dominant_model == "codex-auto-review":
        return "guardian/auto-review"
    role, conf = session.inferred_role
    return role if role != "unknown" else "unknown"


def role_for_key(key: str, family: finder.Family, sessions: Dict[str, finder.Session]) -> str:
    if key == family.root:
        return role_for_session(sessions[key]) + "/root"
    return role_for_session(sessions[key])


def direct_parent_map(family: finder.Family) -> Dict[str, str]:
    return {e.child: e.parent for e in family.edges}


def build_id_indexes(family: finder.Family, sessions: Dict[str, finder.Session]) -> Tuple[dict, dict]:
    own: Dict[str, set[str]] = defaultdict(set)
    any_ids: Dict[str, set[str]] = defaultdict(set)
    for key in family.members:
        s = sessions[key]
        for ident in s.links.own_ids:
            own[ident].add(key)
            any_ids[ident].add(key)
        for ident in s.links.parent_ids:
            any_ids[ident].add(key)
    return own, any_ids


def candidate_score(action: ActionEvent, candidate: finder.Session, candidate_key: str,
                    caller_key: str, direct_parent: Dict[str, str], id_source: str,
                    role_hints: Counter) -> float:
    score = 0.0
    if id_source == "own":
        score += 100
    elif id_source == "any":
        score += 65
    elif id_source == "graph-time":
        score += 35
    elif id_source == "time":
        score += 15

    if direct_parent.get(candidate_key) == caller_key:
        score += 20

    if candidate.first_ts:
        delta = (candidate.first_ts - action.ts).total_seconds()
        if -30 <= delta <= 30 * 60:
            score += max(0.0, 30.0 - abs(delta) / 60.0)
        elif delta < -60:
            score -= min(abs(delta) / 60.0, 30)

    role = role_for_session(candidate)
    if role != "unknown" and role_hints.get(role):
        score += 20
    return score


def match_actions(family: finder.Family, sessions: Dict[str, finder.Session],
                  parsed: Dict[str, ParsedSession], spawn_window_minutes: float,
                  tight_spawn_seconds: float = 2.0) -> None:
    """Resolve lifecycle targets conservatively.

    Trusted attribution requires one of:
      * an action/result ID that matches exactly one session's own linkage ID
      * a unique child session beginning within `tight_spawn_seconds` of a spawn

    Generic parent/thread ancestry and single-active-agent guesses are never used
    for cost attribution. They may be retained as low-confidence diagnostics.
    """
    own_idx, any_idx = build_id_indexes(family, sessions)
    direct_parent = direct_parent_map(family)
    family_set = set(family.members)
    already_spawned: set[str] = set()

    all_actions = sorted(
        (a for key in family.members for a in parsed[key].actions),
        key=lambda a: (a.ts, a.caller_key, a.kind, a.call_id or ""),
    )

    # 1) Exact own-ID evidence. Parent/shared IDs are intentionally excluded from
    # trusted attribution because they can describe ancestry rather than target.
    for action in all_actions:
        ids = set(action.target_ids) | set(action.result_ids)
        candidates: Dict[str, int] = Counter()
        for ident in ids:
            for key in own_idx.get(ident, ()):
                if key == action.caller_key or key not in family_set:
                    continue
                candidates[key] += 1

        if candidates:
            ranked = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
            best_key, best_n = ranked[0]
            unique = len(ranked) == 1 or best_n > ranked[1][1]
            if unique:
                action.matched_session = best_key
                if action.kind == "spawn" and action.result_ids:
                    action.match_method = "spawn-result-id"
                else:
                    action.match_method = "exact-target-id"
                action.match_confidence = "high"
                if action.kind == "spawn":
                    already_spawned.add(best_key)
                continue

        # Shared/ancestry IDs are useful diagnostics only.
        linked = set()
        for ident in ids:
            linked |= {k for k in any_idx.get(ident, ()) if k != action.caller_key and k in family_set}
        own_linked = set(candidates)
        linked -= own_linked
        if len(linked) == 1:
            action.diagnostic_session = next(iter(linked))
            action.match_method = "shared-link-id"
            action.match_confidence = "low"

    # 2) Unresolved spawn calls: very tight start-time evidence can be trusted.
    # A child beginning within milliseconds/seconds of a spawn is much stronger
    # than the old broad graph-time fallback.
    broad_window = spawn_window_minutes * 60
    for action in [a for a in all_actions if a.kind == "spawn" and not is_trusted_action(a)]:
        tight = []
        broad = []
        for key in family.members:
            if key == action.caller_key or key in already_spawned:
                continue
            s = sessions[key]
            if not s.first_ts:
                continue
            delta = (s.first_ts - action.ts).total_seconds()
            if -0.5 <= delta <= tight_spawn_seconds:
                tight.append((abs(delta), key))
            if -15 <= delta <= broad_window:
                broad.append((abs(delta), key))

        tight.sort()
        # Require a unique tight candidate. If two sessions start nearly together,
        # keep the spawn unresolved rather than guess.
        if len(tight) == 1:
            key = tight[0][1]
            action.matched_session = key
            action.diagnostic_session = None
            action.match_method = "tight-start-time"
            action.match_confidence = "medium"
            already_spawned.add(key)
            continue
        if len(tight) > 1 and len(tight) >= 2 and (tight[1][0] - tight[0][0]) >= 0.75:
            key = tight[0][1]
            action.matched_session = key
            action.diagnostic_session = None
            action.match_method = "tight-start-time"
            action.match_confidence = "medium"
            already_spawned.add(key)
            continue

        # Broad graph/time evidence is diagnostic only.
        graph = [x for x in broad if direct_parent.get(x[1]) == action.caller_key]
        diag = graph if graph else broad
        diag.sort()
        if len(diag) == 1:
            action.diagnostic_session = diag[0][1]
            action.match_method = "graph-time-diagnostic" if graph else "time-diagnostic"
            action.match_confidence = "low"

    # 3) Unresolved send/resume/wait/close. Retain single-active-agent as a
    # diagnostic hint only and never allow caller -> caller self-targets.
    spawned_sessions = {
        a.matched_session for a in all_actions
        if a.kind == "spawn" and is_trusted_action(a) and a.matched_session
    }
    for action in [a for a in all_actions
                   if a.kind in {"send", "resume", "wait", "close"}
                   and not is_trusted_action(a)]:
        if action.diagnostic_session == action.caller_key:
            action.diagnostic_session = None
        if action.diagnostic_session:
            continue
        active = []
        for key in spawned_sessions:
            if key is None or key == action.caller_key:
                continue
            s = sessions[key]
            if s.first_ts and s.last_ts and s.first_ts <= action.ts <= s.last_ts + timedelta(minutes=2):
                active.append(key)
        if len(active) == 1:
            action.diagnostic_session = active[0]
            action.match_method = "single-active-agent"
            action.match_confidence = "low"


def assign_agent_labels(family: finder.Family, sessions: Dict[str, finder.Session],
                        parsed: Dict[str, ParsedSession]) -> Dict[str, str]:
    # Label children by the earliest matched spawn, then by observed first time.
    spawn_time: Dict[str, datetime] = {}
    for key in family.members:
        for action in parsed[key].actions:
            if action.kind == "spawn" and action.matched_session:
                spawn_time.setdefault(action.matched_session, action.ts)

    children = [k for k in family.members if k != family.root]
    children.sort(key=lambda k: (
        spawn_time.get(k) or sessions[k].first_ts or datetime.max.replace(tzinfo=timezone.utc),
        k,
    ))
    labels = {family.root: "ROOT"}
    for i, key in enumerate(children, 1):
        labels[key] = f"A{i:02d}"
    return labels


def incoming_actions(parsed: Dict[str, ParsedSession], family: finder.Family) -> Dict[str, List[ActionEvent]]:
    """Trusted incoming lifecycle actions only."""
    incoming: Dict[str, List[ActionEvent]] = defaultdict(list)
    for key in family.members:
        for action in parsed[key].actions:
            if is_trusted_action(action) and action.matched_session:
                incoming[action.matched_session].append(action)
    for events in incoming.values():
        events.sort(key=lambda a: a.ts)
    return incoming


def build_phases(family: finder.Family, sessions: Dict[str, finder.Session],
                 parsed: Dict[str, ParsedSession], labels: Dict[str, str]) -> List[AgentPhase]:
    incoming = incoming_actions(parsed, family)
    phases: Dict[Tuple[str, str], AgentPhase] = {}

    for key in family.members:
        if key == family.root:
            continue
        role = role_for_key(key, family, sessions)
        sends = [a for a in incoming.get(key, []) if a.kind in {"send", "resume"}]
        sends.sort(key=lambda a: a.ts)

        for req in parsed[key].usage:
            round_no = sum(1 for a in sends if a.ts <= req.ts)
            phase_name = "initial" if round_no == 0 else f"follow-up {round_no}"
            pkey = (key, phase_name)
            if pkey not in phases:
                phases[pkey] = AgentPhase(
                    session_key=key,
                    agent_label=labels[key],
                    role=role,
                    phase=phase_name,
                )
            phase = phases[pkey]
            phase.request_count += 1
            phase.input_tokens += req.input_tokens
            phase.cached_input_tokens += req.cached_input_tokens
            phase.output_tokens += req.output_tokens
            phase.reasoning_tokens += req.reasoning_tokens

    return sorted(phases.values(), key=lambda p: (p.agent_label, 0 if p.phase == "initial" else 1, p.phase))


def nearest_action_decisions(family: finder.Family, parsed: Dict[str, ParsedSession],
                             labels: Dict[str, str], sessions: Dict[str, finder.Session],
                             window_seconds: float) -> List[ParentDecision]:
    """Pair parent inference requests to nearby lifecycle actions heuristically.

    This does not claim the request caused the action. It is a diagnostic for
    expensive parent inference around spawn/send/wait/close decisions.
    """
    out: List[ParentDecision] = []
    for caller in family.members:
        actions = [a for a in parsed[caller].actions if a.kind in {"spawn", "send", "resume", "wait", "close"}]
        if not actions:
            continue
        usage = parsed[caller].usage
        if not usage:
            continue
        used_req: set[int] = set()
        for action in actions:
            candidates = []
            for i, req in enumerate(usage):
                if i in used_req:
                    continue
                delta = (action.ts - req.ts).total_seconds()
                # Prefer a request immediately before the action, but tolerate
                # logging order differences by allowing a short post-action gap.
                if -15 <= delta <= window_seconds:
                    penalty = abs(delta) + (0 if delta >= 0 else 20)
                    candidates.append((penalty, abs(delta), i, req))
            if not candidates:
                continue
            candidates.sort(key=lambda x: (x[0], x[1]))
            _, abs_delta, i, req = candidates[0]
            used_req.add(i)
            target_label = labels.get(action.matched_session or "", "?") if is_trusted_action(action) else "?"
            role = role_for_key(action.matched_session, family, sessions) if is_trusted_action(action) and action.matched_session else "unknown"
            out.append(ParentDecision(req, action, target_label, role, abs_delta))
    return out


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.1f}K"
    return str(n)


def fmt_pct(x: float) -> str:
    return f"{100*x:.0f}%" if math.isfinite(x) else "-"


def local_time(dt: Optional[datetime]) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else "?"


def duration_text(a: Optional[datetime], b: Optional[datetime]) -> str:
    if not a or not b:
        return "?"
    seconds = max(0.0, (b - a).total_seconds())
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def resolve_family(selector: str, families: Sequence[finder.Family],
                   sessions: Dict[str, finder.Session]) -> Optional[finder.Family]:
    """Resolve a family key, hashed member key or exact raw session/thread ID.

    Raw IDs are fingerprinted locally, never exported. Prefer the rollout-derived
    session key; metadata is a fallback and must identify exactly one family.
    Selection finds the containing family, not a new analysis root or subtree.
    """
    selector = selector.strip()
    for fam in families:
        if fam.family_key == selector:
            return fam
    if selector.startswith("S-"):
        for fam in families:
            if selector in fam.members:
                return fam
        return None
    if selector.startswith("W-"):
        return None
    ident = finder.fp(selector)
    if ident is None:
        return None
    key = finder.short_key("S", ident)
    matches = [fam for fam in families if key in fam.members]
    if not matches:
        matches = [fam for fam in families if any(
            ident in sessions[member].links.own_ids for member in fam.members)]
    if len(matches) == 1:
        return matches[0]
    return None


def action_coverage(family: finder.Family, parsed: Dict[str, ParsedSession]) -> Counter:
    c = Counter()
    for key in family.members:
        c["duplicate_actions_removed"] += parsed[key].duplicate_actions_removed
        for a in parsed[key].actions:
            c[f"{a.kind}_total"] += 1
            if a.call_id:
                c["actions_with_call_id"] += 1
            if a.target_ids:
                c["actions_with_target_ids"] += 1
            if a.result_ids:
                c["actions_with_result_ids"] += 1
            cls = attribution_class(a)
            c[f"{a.kind}_{cls}"] += 1
            c[f"class_{cls}"] += 1
            if cls != "unresolved":
                c[f"method_{a.match_method}"] += 1
            if is_trusted_action(a):
                c[f"confidence_{a.match_confidence}"] += 1
    return c


def role_costs(family: finder.Family, sessions: Dict[str, finder.Session],
               parsed: Dict[str, ParsedSession]) -> Dict[str, Counter]:
    out: Dict[str, Counter] = defaultdict(Counter)
    for key in family.members:
        role = role_for_key(key, family, sessions)
        for req in parsed[key].usage:
            out[role]["requests"] += 1
            out[role]["input"] += req.input_tokens
            out[role]["cached"] += req.cached_input_tokens
            out[role]["uncached"] += req.uncached_input_tokens
            out[role]["output"] += req.output_tokens
            out[role]["reasoning"] += req.reasoning_tokens
    return out


def top_expensive_requests(family: finder.Family, sessions: Dict[str, finder.Session],
                           parsed: Dict[str, ParsedSession], labels: Dict[str, str],
                           n: int) -> List[Tuple[UsageRequest, str, str]]:
    rows = []
    for key in family.members:
        role = role_for_key(key, family, sessions)
        for req in parsed[key].usage:
            rows.append((req, labels[key], role))
    rows.sort(key=lambda x: x[0].total_tokens, reverse=True)
    return rows[:n]


def large_context_summary(family: finder.Family, sessions: Dict[str, finder.Session],
                          parsed: Dict[str, ParsedSession], input_threshold: int,
                          output_threshold: int) -> Tuple[Counter, List[Tuple[UsageRequest, str]]]:
    c = Counter()
    rows = []
    for key in family.members:
        role = role_for_key(key, family, sessions)
        for req in parsed[key].usage:
            if req.input_tokens >= input_threshold and req.output_tokens <= output_threshold:
                c["requests"] += 1
                c["input"] += req.input_tokens
                c["cached"] += req.cached_input_tokens
                c["uncached"] += req.uncached_input_tokens
                c["output"] += req.output_tokens
                c[f"role:{role}"] += req.input_tokens
                rows.append((req, role))
    rows.sort(key=lambda x: x[0].input_tokens, reverse=True)
    return c, rows


def print_family_report(family: finder.Family, sessions: Dict[str, finder.Session],
                        parsed: Dict[str, ParsedSession], labels: Dict[str, str],
                        args: argparse.Namespace) -> None:
    first = family.first_ts(sessions)
    last = family.last_ts(sessions)
    coverage = action_coverage(family, parsed)
    phases = build_phases(family, sessions, parsed, labels)
    decisions = nearest_action_decisions(
        family, parsed, labels, sessions, args.decision_window_seconds
    )

    print(f"\nWorkflow {family.family_key}")
    print("=" * (9 + len(family.family_key)))
    print(f"span:      {local_time(first)} -> {local_time(last)} ({duration_text(first, last)})")
    print(f"sessions:  {len(family.members)}")
    print(f"root:      {labels[family.root]} / {family.root}")
    print("Privacy: raw IDs, prompts, responses, source code and tool output are not shown.")

    print("\nLifecycle match audit")
    print("---------------------")
    print("trusted = exact target/result ID or unique very-tight spawn/start match")
    print("diagnostic = hint only; never used for follow-up or cost attribution")
    print(f"{'action':<8} {'trusted':>9} {'diagnostic':>11} {'unresolved':>11} {'total':>7}")
    for kind in ("spawn", "send", "resume", "wait", "close"):
        total = coverage.get(f"{kind}_total", 0)
        if not total:
            continue
        trusted = coverage.get(f"{kind}_trusted", 0)
        diagnostic = coverage.get(f"{kind}_diagnostic", 0)
        unresolved = coverage.get(f"{kind}_unresolved", 0)
        print(f"{kind:<8} {trusted:>9,} {diagnostic:>11,} {unresolved:>11,} {total:>7,}")
    methods = [(k[7:], v) for k, v in coverage.items() if k.startswith("method_")]
    if methods:
        print("methods: " + ", ".join(f"{k}={v}" for k, v in sorted(methods)))
    action_total = sum(coverage.get(f"{k}_total", 0) for k in ("spawn", "send", "resume", "wait", "close"))
    if action_total:
        print(
            "structural IDs: "
            f"call_id={coverage.get('actions_with_call_id',0)}/{action_total}, "
            f"target_ids={coverage.get('actions_with_target_ids',0)}/{action_total}, "
            f"result_ids={coverage.get('actions_with_result_ids',0)}/{action_total}"
        )
    if coverage.get("duplicate_actions_removed"):
        print(f"duplicate action representations removed: {coverage['duplicate_actions_removed']:,}")
    print("Only trusted actions can create follow-up phases or role-targeted supervision totals.")

    print("\nRole / session cost")
    print("-------------------")
    costs = role_costs(family, sessions, parsed)
    total_input = sum(c["input"] for c in costs.values()) or 1
    total_tokens = sum(c["input"] + c["output"] for c in costs.values()) or 1
    print(f"{'role':<24} {'req':>6} {'input':>10} {'cached':>8} {'uncached':>10} {'output':>9} {'share':>7}")
    for role, c in sorted(costs.items(), key=lambda kv: -(kv[1]["input"] + kv[1]["output"])):
        cache_share = c["cached"] / c["input"] if c["input"] else float("nan")
        share = (c["input"] + c["output"]) / total_tokens
        print(
            f"{role:<24} {c['requests']:>6,} {fmt_tokens(c['input']):>10} "
            f"{fmt_pct(cache_share):>8} {fmt_tokens(c['uncached']):>10} "
            f"{fmt_tokens(c['output']):>9} {100*share:>6.1f}%"
        )

    print("\nAgent lifecycle cost")
    print("--------------------")
    incoming = incoming_actions(parsed, family)
    for key in sorted((k for k in family.members if k != family.root),
                      key=lambda k: labels[k]):
        s = sessions[key]
        role = role_for_key(key, family, sessions)
        sends = [a for a in incoming.get(key, []) if a.kind in {"send", "resume"}]
        spawns = [a for a in incoming.get(key, []) if a.kind == "spawn"]
        sess_phases = [p for p in phases if p.session_key == key]
        toks = sum(p.total_tokens for p in sess_phases)
        print(
            f"{labels[key]:<4} role={role:<20} model={s.dominant_model:<17} "
            f"tokens={fmt_tokens(toks):>8} trusted_followups={len(sends):<3} "
            f"trusted_spawn={'yes' if spawns else 'no'}"
        )
        for p in sess_phases:
            cache_share = p.cached_input_tokens / p.input_tokens if p.input_tokens else float("nan")
            print(
                f"     {p.phase:<12} req={p.request_count:<4} input={fmt_tokens(p.input_tokens):>8} "
                f"cache={fmt_pct(cache_share):>4} output={fmt_tokens(p.output_tokens):>7} "
                f"total={fmt_tokens(p.total_tokens):>8}"
            )

    print("\nParent action-associated inference")
    print("----------------------------------")
    if not decisions:
        print("No parent inference requests could be paired to lifecycle actions within the configured window.")
    else:
        by_kind: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
        for d in decisions:
            role = d.target_role if d.target_role != "unknown" else "unknown target"
            c = by_kind[(d.action.kind, role)]
            c["requests"] += 1
            c["input"] += d.request.input_tokens
            c["cached"] += d.request.cached_input_tokens
            c["output"] += d.request.output_tokens
        print("Heuristic: nearest caller inference around each lifecycle action; this is not a causal claim.")
        print("Target role is shown only for trusted lifecycle matches; diagnostic/unresolved targets remain unknown.")
        print(f"{'action / target':<34} {'req':>5} {'input':>10} {'cached':>8} {'output':>9}")
        for (kind, role), c in sorted(by_kind.items(), key=lambda kv: -kv[1]["input"]):
            cache_share = c["cached"] / c["input"] if c["input"] else float("nan")
            print(
                f"{(kind + ' -> ' + role):<34} {c['requests']:>5,} {fmt_tokens(c['input']):>10} "
                f"{fmt_pct(cache_share):>8} {fmt_tokens(c['output']):>9}"
            )

    print("\nLarge-context / small-output requests")
    print("-------------------------------------")
    large, large_rows = large_context_summary(
        family, sessions, parsed,
        args.large_context_input_tokens,
        args.small_output_tokens,
    )
    if not large.get("requests"):
        print(
            f"None with input >= {fmt_tokens(args.large_context_input_tokens)} and "
            f"output <= {fmt_tokens(args.small_output_tokens)}."
        )
    else:
        cache_share = large["cached"] / large["input"] if large["input"] else float("nan")
        print(
            f"{large['requests']:,} requests; input={fmt_tokens(large['input'])}, "
            f"cached={fmt_pct(cache_share)}, uncached={fmt_tokens(large['uncached'])}, "
            f"output={fmt_tokens(large['output'])}"
        )
        role_rows = [(k[5:], v) for k, v in large.items() if k.startswith("role:")]
        role_rows.sort(key=lambda x: -x[1])
        if role_rows:
            print("input by role: " + ", ".join(f"{r}={fmt_tokens(v)}" for r, v in role_rows))
        print("Largest examples:")
        for req, role in large_rows[: min(args.top_expensive, 10)]:
            label = labels.get(req.session_key, "?")
            print(
                f"  {local_time(req.ts)} {label:<4} {role:<20} "
                f"input={fmt_tokens(req.input_tokens):>8} cache={fmt_pct(req.cache_share):>4} "
                f"output={fmt_tokens(req.output_tokens):>7}"
            )

    print("\nMost expensive individual inference requests")
    print("--------------------------------------------")
    for req, label, role in top_expensive_requests(
        family, sessions, parsed, labels, args.top_expensive
    ):
        print(
            f"{local_time(req.ts)} {label:<4} {role:<20} model={req.model:<16} "
            f"input={fmt_tokens(req.input_tokens):>8} cache={fmt_pct(req.cache_share):>4} "
            f"output={fmt_tokens(req.output_tokens):>7} total={fmt_tokens(req.total_tokens):>8}"
        )

    print("\nStructural timeline")
    print("-------------------")
    timeline = []
    for key in family.members:
        s = sessions[key]
        if s.first_ts:
            timeline.append((s.first_ts, 1, f"START {labels[key]} role={role_for_key(key, family, sessions)} model={s.dominant_model}"))
        if s.last_ts:
            timeline.append((s.last_ts, 9, f"END   {labels[key]} role={role_for_key(key, family, sessions)}"))
        for action in parsed[key].actions:
            caller = labels.get(key, "?")
            target_key = displayed_target(action)
            target = labels.get(target_key or "", "?")
            role = role_for_key(target_key, family, sessions) if target_key else "unknown"
            cls = attribution_class(action)
            timeline.append((
                action.ts, 5,
                f"{caller:<4} {action.kind.upper():<6} {target:<4} role={role:<20} "
                f"match={action.match_method}/{action.match_confidence} [{cls}]"
            ))
    timeline.sort(key=lambda x: (x[0], x[1], x[2]))
    if len(timeline) > args.timeline_limit:
        print(f"Showing first {args.timeline_limit} of {len(timeline)} structural events. Use --timeline-limit to change.")
    for ts, _, text in timeline[: args.timeline_limit]:
        print(f"{local_time(ts)}  {text}")


def export_report_json(path: str, family: finder.Family, sessions: Dict[str, finder.Session],
                       parsed: Dict[str, ParsedSession], labels: Dict[str, str],
                       args: argparse.Namespace) -> None:
    phases = build_phases(family, sessions, parsed, labels)
    decisions = nearest_action_decisions(
        family, parsed, labels, sessions, args.decision_window_seconds
    )

    payload = {
        "schema": "codex-workflow-lifecycle-v2",
        "version": __version__,
        "family": family.family_key,
        "privacy": "No prompts/responses/source code/tool output/raw IDs are included.",
        "span": {
            "start": family.first_ts(sessions).isoformat() if family.first_ts(sessions) else None,
            "end": family.last_ts(sessions).isoformat() if family.last_ts(sessions) else None,
        },
        "action_match_audit": dict(action_coverage(family, parsed)),
        "sessions": [],
        "actions": [],
        "phases": [],
        "parent_action_associated_inference": [],
    }

    # Local import avoids a module cycle; both tools use the same parent rules.
    import workflow_attribution as attribution
    identities = attribution.resolve(family, sessions, parsed, finder.DEFAULT_ROLES)
    for key in sorted(family.members, key=lambda k: labels[k]):
        s = sessions[key]
        role = role_for_key(key, family, sessions)
        payload["sessions"].append({
            "label": labels[key],
            "session_key": key,
            "role": role,
            "root": key == family.root,
            "responsibility": identities[key]["role"],
            "role_confidence": identities[key]["role_confidence"],
            "immediate_parent": labels.get(identities[key]["parent"]),
            "parent_source": identities[key]["parent_source"],
            "identity_issues": identities[key]["issues"],
            "source_kind": s.source_kind,
            "model": s.dominant_model,
            "effort": s.dominant_effort,
            "start": s.first_ts.isoformat() if s.first_ts else None,
            "end": s.last_ts.isoformat() if s.last_ts else None,
            "usage_requests": len(parsed[key].usage),
        })
        for action in parsed[key].actions:
            payload["actions"].append({
                "timestamp": action.ts.isoformat(),
                "caller": labels[key],
                "kind": action.kind,
                "target": labels.get(action.matched_session or "") if is_trusted_action(action) else None,
                "target_role": role_for_key(action.matched_session, family, sessions) if is_trusted_action(action) and action.matched_session else None,
                "diagnostic_target": labels.get(action.diagnostic_session or "") if action.diagnostic_session else None,
                "attribution": attribution_class(action),
                "match_method": action.match_method,
                "match_confidence": action.match_confidence,
            })

    for p in phases:
        payload["phases"].append({
            "agent": p.agent_label,
            "session_key": p.session_key,
            "role": p.role,
            "phase": p.phase,
            "requests": p.request_count,
            "input_tokens": p.input_tokens,
            "cached_input_tokens": p.cached_input_tokens,
            "uncached_input_tokens": max(p.input_tokens - p.cached_input_tokens, 0),
            "output_tokens": p.output_tokens,
            "reasoning_output_tokens": p.reasoning_tokens,
        })

    for d in decisions:
        payload["parent_action_associated_inference"].append({
            "timestamp": d.request.ts.isoformat(),
            "caller": labels[d.request.session_key],
            "action": d.action.kind,
            "target": d.target_label,
            "target_role": d.target_role,
            "delta_seconds": round(d.delta_seconds, 3),
            "input_tokens": d.request.input_tokens,
            "cached_input_tokens": d.request.cached_input_tokens,
            "output_tokens": d.request.output_tokens,
        })

    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def self_test() -> None:
    # Structural action extraction from a synthetic spawn call + output.
    payload = {
        "type": "function_call",
        "name": "spawn_agent",
        "call_id": "call-123456789",
        "arguments": json.dumps({
            "agent_type": "implementer",
            "message": "secret source-code instructions that must never be printed",
        }),
    }
    nodes = list(action_node_candidates(payload))
    assert len(nodes) == 1
    assert classify_action(nodes[0]["name"]) == "spawn"
    hints = collect_role_hints(parse_jsonish(nodes[0]["arguments"]), finder.DEFAULT_ROLES)
    assert hints["implementer"] == 1

    output_payload = {
        "type": "function_call_output",
        "call_id": "call-123456789",
        "output": json.dumps({"agent_id": "agent_abcdef123456"}),
    }
    rec = extract_output_record(output_payload)
    assert rec is not None and rec[1]

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    root = finder.Session("/tmp/root", "S-root", source_kind="cli")
    child = finder.Session("/tmp/child", "S-child", source_kind="subagent")
    child.roles.self_role["implementer"] = 3
    root.first_ts = now; root.last_ts = now + timedelta(minutes=10)
    child.first_ts = now + timedelta(minutes=1); child.last_ts = now + timedelta(minutes=9)
    child_id = hash_id("agent_abcdef123456")
    assert child_id
    child.links.own_ids.add(child_id)
    fam = finder.Family(["S-root", "S-child"], "S-root", [], "W-test")

    trusted_send = ActionEvent(
        ts=now + timedelta(minutes=5), caller_key="S-root", kind="send", name="send_input",
        call_id="call-send", target_ids={child_id}
    )
    duplicate_send = ActionEvent(
        ts=now + timedelta(minutes=5), caller_key="S-root", kind="send", name="send_input",
        call_id="call-send", target_ids={child_id}
    )
    ps = {
        "S-root": ParsedSession(actions=[trusted_send, duplicate_send]),
        "S-child": ParsedSession(usage=[
            UsageRequest(now + timedelta(minutes=2), "S-child", 1000, 800, 100, 0, "m", "high"),
            UsageRequest(now + timedelta(minutes=6), "S-child", 2000, 1800, 200, 0, "m", "high"),
        ]),
    }
    ps["S-root"].actions, ps["S-root"].duplicate_actions_removed = dedupe_actions(ps["S-root"].actions)
    assert ps["S-root"].duplicate_actions_removed == 1
    match_actions(fam, {"S-root": root, "S-child": child}, ps, 30.0, 2.0)
    assert ps["S-root"].actions[0].matched_session == "S-child"
    assert ps["S-root"].actions[0].match_confidence == "high"

    phases = build_phases(fam, {"S-root": root, "S-child": child}, ps, {"S-root": "ROOT", "S-child": "A01"})
    assert [p.phase for p in phases] == ["initial", "follow-up 1"]
    assert phases[0].total_tokens == 1100 and phases[1].total_tokens == 2200

    # Low-confidence single-active-agent hints must not create follow-up phases,
    # and caller -> caller self-targeting is forbidden.
    low = ActionEvent(
        ts=now + timedelta(minutes=5), caller_key="S-child", kind="send", name="send_input",
        call_id=None, diagnostic_session="S-child", match_method="single-active-agent", match_confidence="low"
    )
    assert not is_trusted_action(low)
    if low.diagnostic_session == low.caller_key:
        low.diagnostic_session = None
    assert low.diagnostic_session is None

    guardian = finder.Session("/tmp/g", "S-g", source_kind="subagent")
    guardian.models["codex-auto-review"] = 1
    assert role_for_session(guardian) == "guardian/auto-review"
    print("self-test: OK")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Extract a privacy-safe structural lifecycle and token-cost profile "
            "for one workflow family found by find_workflow_candidates.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 extract_workflow_lifecycle.py --family W-e7af89b98f
  python3 extract_workflow_lifecycle.py --family S-644a110a4f
  python3 extract_workflow_lifecycle.py --family W-e7af89b98f --export-json workflow_lifecycle.json
  python3 extract_workflow_lifecycle.py --family W-e7af89b98f --timeline-limit 400
  python3 extract_workflow_lifecycle.py --self-test

Tip: a stable S-... session key can be used if a W-... family ID changes after
new linked rollout files are added to ~/.codex.
""",
    )
    ap.add_argument("--family", "--session", "--session-id", dest="family", action="append", metavar="ID",
                    help="family W-..., member S-..., or exact Codex session/thread ID; repeatable")
    ap.add_argument("--home", default=os.path.expanduser("~/.codex"), help="Codex data directory")
    ap.add_argument("--roles", nargs="+", default=list(finder.DEFAULT_ROLES),
                    help="role labels to recognize")
    ap.add_argument("--spawn-window-minutes", type=float, default=30.0,
                    help="broad diagnostic window for unresolved spawn calls")
    ap.add_argument("--tight-spawn-seconds", type=float, default=2.0,
                    help="unique child-start window trusted as medium-confidence spawn evidence")
    ap.add_argument("--decision-window-seconds", type=float, default=120.0,
                    help="pair parent inference to lifecycle actions within this window")
    ap.add_argument("--large-context-input-tokens", type=int, default=200_000,
                    help="large-context threshold for optimization candidates")
    ap.add_argument("--small-output-tokens", type=int, default=2_000,
                    help="small-output threshold for optimization candidates")
    ap.add_argument("--top-expensive", type=int, default=15,
                    help="number of expensive individual inference requests to show")
    ap.add_argument("--timeline-limit", type=int, default=250,
                    help="maximum structural timeline rows per workflow")
    ap.add_argument("--export-json", metavar="PATH",
                    help="write privacy-safe lifecycle JSON (one family only)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    if not args.family:
        ap.error("at least one --family ID or --session SESSION_ID is required")
    if args.export_json and len(args.family) != 1:
        ap.error("--export-json currently requires exactly one --family selector")

    roles = tuple(dict.fromkeys(r.strip().lower() for r in args.roles if r.strip()))
    home = os.path.abspath(os.path.expanduser(args.home))
    paths = finder.file_paths(home)

    print(f"Codex workflow lifecycle extractor v{__version__}")
    print("======================================")
    print("Source: ~/.codex" if home == os.path.abspath(os.path.expanduser("~/.codex")) else f"Source: {home}")
    print(f"Files: {len(paths):,}")
    print(f"Roles: {', '.join(roles)}")
    print("Rebuilding privacy-safe family graph so W-/S- selectors can be resolved...\n")

    sessions: Dict[str, finder.Session] = {}
    for idx, path in enumerate(paths, 1):
        s = finder.scan_session(path, roles)
        key = s.session_key
        if key in sessions:
            key = finder.short_key("S", path)
            s.session_key = key
        sessions[key] = s
        if idx % 500 == 0:
            print(f"scanned {idx:,}/{len(paths):,} files...", flush=True)

    edges = finder.build_edges(sessions)
    families = finder.build_families(sessions, edges)

    selected: List[finder.Family] = []
    for selector in args.family:
        fam = resolve_family(selector, families, sessions)
        if fam is None:
            print("\nCould not uniquely resolve the supplied family/session selector against the current log graph.", file=sys.stderr)
            print(
                "If this was an older W-... ID, rerun find_workflow_candidates.py and use "
                "the stable ROOT S-... session key from the earlier output.",
                file=sys.stderr,
            )
            return 2
        if fam not in selected:
            selected.append(fam)

    for fam_index, family in enumerate(selected, 1):
        print(f"\nParsing {len(family.members)} rollout files for {family.family_key}...")
        parsed: Dict[str, ParsedSession] = {}
        for key in family.members:
            parsed[key] = parse_family_session(sessions[key].path, key, roles)

        match_actions(family, sessions, parsed, args.spawn_window_minutes, args.tight_spawn_seconds)
        labels = assign_agent_labels(family, sessions, parsed)
        print_family_report(family, sessions, parsed, labels, args)

        if args.export_json:
            export_report_json(args.export_json, family, sessions, parsed, labels, args)
            print(f"\nWrote privacy-safe lifecycle JSON: {args.export_json}")

    print("\nNext step")
    print("---------")
    print(
        "Share this output (or the --export-json file). v2 separates trusted lifecycle attribution "
        "from diagnostic guesses, so we can decide which workflow costs are safe to optimize against."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
