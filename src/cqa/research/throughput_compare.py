#!/usr/bin/env python3
"""Compare CQA visible-generation throughput with a Tokscale-style reconstruction.

This module is deliberately research-only.  It reads the same local Codex JSONL
rollouts that CQA already analyzes, retains no prompt/response/tool content, and
writes only aggregate or report-local identifiers.  Existing CQA dashboard and
cqa-report-v1 analytics are not changed by this module.

The Tokscale-style side mirrors the important timing semantics of the current
Tokscale Codex parser as inspected for this release: ``turn_context`` (and a
human ``user_message`` fallback) starts a timing cursor, accepted ``token_count``
records close non-overlapping intervals, duplicate/obviously stale cumulative
snapshots are ignored, and output throughput uses non-reasoning output tokens.
It is intentionally labelled *Tokscale-style*: this implementation does not
claim byte-for-byte parity with every upstream fork/replay/deduplication rule.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import shlex
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..workflow import attribution
from ..workflow import candidates as finder
from ..workflow import lifecycle
from ..workflow import profile as profiler
from ..workflow.response_efficiency import (
    ToolExcludedTask, aggregate_tool_excluded as _aggregate_tool_excluded,
    parse_tool_excluded_tasks,
)

SCHEMA = "cqa-throughput-comparison"
SCHEMA_VERSION = "1.0.0"
GENERATOR_VERSION = "0.9.1"


@dataclass(frozen=True)
class TokenTotals:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @classmethod
    def from_obj(cls, obj: object) -> Optional["TokenTotals"]:
        if not isinstance(obj, dict):
            return None
        try:
            return cls(
                input_tokens=max(0, int(obj.get("input_tokens", 0) or 0)),
                cached_input_tokens=max(0, int(obj.get("cached_input_tokens", 0) or 0)),
                output_tokens=max(0, int(obj.get("output_tokens", 0) or 0)),
                reasoning_output_tokens=max(0, int(obj.get("reasoning_output_tokens", 0) or 0)),
            )
        except (TypeError, ValueError):
            return None

    @property
    def total_tokens(self) -> int:
        # Mirrors Tokscale's CodexTotals::total() stale-regression heuristic,
        # which deliberately sums all four counters even though cache/reasoning
        # are subsets for pricing/accounting purposes.
        return (self.input_tokens + self.output_tokens
                + self.cached_input_tokens + self.reasoning_output_tokens)

    @property
    def nonreasoning_output_tokens(self) -> int:
        return max(self.output_tokens - self.reasoning_output_tokens, 0)

    @property
    def is_zero(self) -> bool:
        return self.total_tokens == 0 and self.cached_input_tokens == 0 and self.reasoning_output_tokens == 0

    def delta_from(self, previous: "TokenTotals") -> Optional["TokenTotals"]:
        values = (
            self.input_tokens - previous.input_tokens,
            self.cached_input_tokens - previous.cached_input_tokens,
            self.output_tokens - previous.output_tokens,
            self.reasoning_output_tokens - previous.reasoning_output_tokens,
        )
        if min(values) < 0:
            return None
        return TokenTotals(*values)

    def add(self, other: "TokenTotals") -> "TokenTotals":
        return TokenTotals(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_output_tokens + other.reasoning_output_tokens,
        )


@dataclass(frozen=True)
class TimedEntry:
    ts: datetime
    model: str
    effort: str
    tokens: TokenTotals
    duration_seconds: Optional[float]
    first_interval: bool
    tool_signal_interval: bool
    tool_result_interval: bool


@dataclass
class ParseDiagnostics:
    json_errors: int = 0
    token_count_records: int = 0
    accepted_entries: int = 0
    untimed_entries: int = 0
    duplicate_cumulative: int = 0
    stale_regression: int = 0
    zero_token_snapshot: int = 0
    missing_usage: int = 0
    missing_timestamp: int = 0
    cursor_resets_turn_context: int = 0
    cursor_resets_user_message: int = 0

    def as_dict(self) -> dict:
        return {
            "json_errors": self.json_errors,
            "token_count_records": self.token_count_records,
            "accepted_entries": self.accepted_entries,
            "untimed_entries": self.untimed_entries,
            "duplicate_cumulative": self.duplicate_cumulative,
            "stale_regression": self.stale_regression,
            "zero_token_snapshot": self.zero_token_snapshot,
            "missing_usage": self.missing_usage,
            "missing_timestamp": self.missing_timestamp,
            "cursor_resets_turn_context": self.cursor_resets_turn_context,
            "cursor_resets_user_message": self.cursor_resets_user_message,
        }


@dataclass
class StyleAccumulator:
    accepted_entries: int = 0
    timed_entries: int = 0
    untimed_entries: int = 0
    output_tokens: int = 0
    timed_output_tokens: int = 0
    timed_duration_seconds: float = 0.0
    first_interval_entries: int = 0
    first_interval_output_tokens: int = 0
    first_interval_seconds: float = 0.0
    tool_signal_entries: int = 0
    tool_signal_output_tokens: int = 0
    tool_signal_seconds: float = 0.0
    tool_result_entries: int = 0
    tool_result_output_tokens: int = 0
    tool_result_seconds: float = 0.0
    later_output_tokens: int = 0
    later_seconds: float = 0.0
    no_tool_signal_output_tokens: int = 0
    no_tool_signal_seconds: float = 0.0
    no_tool_result_output_tokens: int = 0
    no_tool_result_seconds: float = 0.0

    def add(self, entry: TimedEntry) -> None:
        self.accepted_entries += 1
        visible = entry.tokens.nonreasoning_output_tokens
        self.output_tokens += visible
        duration = entry.duration_seconds
        if duration is None or duration <= 0:
            self.untimed_entries += 1
            return
        self.timed_entries += 1
        self.timed_output_tokens += visible
        self.timed_duration_seconds += duration
        if entry.first_interval:
            self.first_interval_entries += 1
            self.first_interval_output_tokens += visible
            self.first_interval_seconds += duration
        else:
            self.later_output_tokens += visible
            self.later_seconds += duration
        if entry.tool_signal_interval:
            self.tool_signal_entries += 1
            self.tool_signal_output_tokens += visible
            self.tool_signal_seconds += duration
        else:
            self.no_tool_signal_output_tokens += visible
            self.no_tool_signal_seconds += duration
        if entry.tool_result_interval:
            self.tool_result_entries += 1
            self.tool_result_output_tokens += visible
            self.tool_result_seconds += duration
        else:
            self.no_tool_result_output_tokens += visible
            self.no_tool_result_seconds += duration

    @staticmethod
    def _rate(tokens: int, seconds: float) -> Optional[float]:
        return tokens / seconds if seconds > 0 else None

    def finish(self) -> dict:
        duration = self.timed_duration_seconds
        return {
            "accepted_entries": self.accepted_entries,
            "timed_entries": self.timed_entries,
            "untimed_entries": self.untimed_entries,
            "output_tokens": self.output_tokens,
            "timed_output_tokens": self.timed_output_tokens,
            "timed_duration_seconds": duration,
            "output_tokens_per_second": self._rate(self.timed_output_tokens, duration),
            "first_intervals": {
                "entries": self.first_interval_entries,
                "output_tokens": self.first_interval_output_tokens,
                "seconds": self.first_interval_seconds,
                "duration_share": self.first_interval_seconds / duration if duration > 0 else None,
            },
            "later_intervals": {
                "output_tokens": self.later_output_tokens,
                "seconds": self.later_seconds,
                "output_tokens_per_second": self._rate(self.later_output_tokens, self.later_seconds),
            },
            "tool_signal_intervals": {
                "entries": self.tool_signal_entries,
                "output_tokens": self.tool_signal_output_tokens,
                "seconds": self.tool_signal_seconds,
                "duration_share": self.tool_signal_seconds / duration if duration > 0 else None,
            },
            "excluding_tool_signal_intervals": {
                "output_tokens": self.no_tool_signal_output_tokens,
                "seconds": self.no_tool_signal_seconds,
                "output_tokens_per_second": self._rate(self.no_tool_signal_output_tokens, self.no_tool_signal_seconds),
            },
            "tool_result_intervals": {
                "entries": self.tool_result_entries,
                "output_tokens": self.tool_result_output_tokens,
                "seconds": self.tool_result_seconds,
                "duration_share": self.tool_result_seconds / duration if duration > 0 else None,
            },
            "excluding_tool_result_intervals": {
                "output_tokens": self.no_tool_result_output_tokens,
                "seconds": self.no_tool_result_seconds,
                "output_tokens_per_second": self._rate(self.no_tool_result_output_tokens, self.no_tool_result_seconds),
            },
        }


def _parse_ts(value: object) -> Optional[datetime]:
    return finder.parse_ts(value)


def _usage_objects(payload: Mapping[str, object]) -> Tuple[Optional[TokenTotals], Optional[TokenTotals]]:
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    last = TokenTotals.from_obj(info.get("last_token_usage") or payload.get("last_token_usage"))
    total = TokenTotals.from_obj(info.get("total_token_usage") or payload.get("total_token_usage"))
    return last, total


def _looks_like_stale_regression(previous: TokenTotals, current: TokenTotals,
                                 last: Optional[TokenTotals]) -> bool:
    """Conservative mirror of Tokscale's resumed/stale cumulative guard.

    A small backwards movement, or one explainable by roughly one/two recent
    usage deltas, is treated as an out-of-order stale snapshot rather than a new
    cumulative epoch.  Large regressions are allowed to reset the baseline.
    """
    prev = previous.total_tokens
    cur = current.total_tokens
    if prev <= 0 or cur >= prev:
        return False
    recent = last.total_tokens if last is not None else 0
    return (cur * 100 >= prev * 98) or (recent > 0 and cur + recent * 2 >= prev)


def _select_usage(last: Optional[TokenTotals], total: Optional[TokenTotals],
                  previous: Optional[TokenTotals], diagnostics: ParseDiagnostics
                  ) -> Tuple[Optional[TokenTotals], Optional[TokenTotals], bool]:
    """Return (entry tokens, next cumulative baseline, accepted)."""
    if last is None and total is None:
        diagnostics.missing_usage += 1
        return None, previous, False

    if total is not None and previous is not None:
        if total == previous:
            diagnostics.duplicate_cumulative += 1
            return None, previous, False
        delta = total.delta_from(previous)
        if delta is None and _looks_like_stale_regression(previous, total, last):
            diagnostics.stale_regression += 1
            return None, previous, False
        if last is not None:
            tokens = last
            next_previous = total
        elif delta is not None:
            tokens = delta
            next_previous = total
        else:
            # A large cumulative reset with no usable last-token usage cannot be
            # attributed safely; reset the baseline but emit no entry.
            return None, total, False
    elif total is not None:
        tokens = last if last is not None else total
        next_previous = total
    elif last is not None:
        tokens = last
        next_previous = previous.add(last) if previous is not None else None
    else:  # pragma: no cover - guarded above
        return None, previous, False

    if tokens.is_zero:
        diagnostics.zero_token_snapshot += 1
        return None, previous, False
    return tokens, next_previous, True


def _is_human_user_message(top_type: str, payload: Mapping[str, object]) -> bool:
    if top_type != "event_msg" or str(payload.get("type", "")) != "user_message":
        return False
    message = payload.get("message")
    if not isinstance(message, str):
        return False
    text = message.strip()
    # Current Tokscale excludes these known Codex system-injected prefixes from
    # human turn detection; the content itself is never retained.
    system_prefixes = ("<environment_context>", "<system-reminder>", "<user_instructions>")
    return bool(text) and not text.startswith(system_prefixes)


def _tool_signal(top_type: str, payload: Mapping[str, object]) -> Tuple[bool, bool]:
    """Return (any tool signal, tool result/output signal), content-free."""
    ptype = str(payload.get("type", "")).lower()
    item_type = ""
    item = payload.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type", "")).lower()
    candidate = item_type or ptype
    call = (
        top_type == "response_item" and (
            candidate in {"function_call", "custom_tool_call", "tool_call"}
            or candidate.endswith("_call")
        )
    ) or ("call" in candidate and "message" not in candidate)
    result = (
        candidate.endswith(("_output", "_result", "call_output", "call_result"))
        or "tool_result" in candidate
        or "function_call_output" in candidate
        or "custom_tool_call_output" in candidate
    )
    return bool(call or result), bool(result)


def parse_tokscale_style(path: str, *, activation: Optional[datetime] = None) -> Tuple[List[TimedEntry], ParseDiagnostics]:
    """Parse one Codex JSONL file into Tokscale-style non-overlapping entries."""
    diagnostics = ParseDiagnostics()
    entries: List[TimedEntry] = []
    current_model = "unknown"
    current_effort = "unknown"
    previous_total: Optional[TokenTotals] = None
    last_accepted_ts: Optional[datetime] = None
    cursor_source: Optional[str] = None
    first_after_boundary = False
    interval_tool_signal = False
    interval_tool_result = False

    try:
        fh = open(path, "rb")
    except OSError:
        return entries, diagnostics

    with fh:
        for raw in fh:
            # The research parser must see cursor resets and tool signals in
            # addition to token_count records, but never retains their content.
            low = raw.lower()
            if not (
                b'"token_count"' in raw or b'"turn_context"' in raw
                or b'"user_message"' in raw or b'"function_call"' in raw
                or b'"tool_call"' in raw or b'call_output' in low
                or b'call_result' in low or b'tool_result' in low
                or b'"model"' in raw or b'"effort"' in raw or b'"reasoning_effort"' in raw
            ):
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                diagnostics.json_errors += 1
                continue
            if not isinstance(obj, dict):
                continue
            ts = _parse_ts(obj.get("timestamp"))
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            top_type = str(obj.get("type", ""))
            ptype = str(payload.get("type", ""))

            model = finder.model_from_payload(payload)
            if not model and ptype == "token_count" and isinstance(payload.get("info"), dict):
                model = finder.model_from_payload(payload["info"])
            if model:
                current_model = model
            effort = finder.effort_from_payload(payload)
            if effort:
                current_effort = effort

            if ts is not None and activation is not None and ts < activation:
                # Do not let inherited fork history establish a timing cursor.
                continue

            if (top_type == "turn_context" or ptype == "turn_context") and ts is not None:
                last_accepted_ts = ts
                cursor_source = "turn_context"
                first_after_boundary = True
                interval_tool_signal = False
                interval_tool_result = False
                diagnostics.cursor_resets_turn_context += 1
                continue
            if ts is not None and _is_human_user_message(top_type, payload):
                last_accepted_ts = ts
                cursor_source = "user_message"
                first_after_boundary = True
                interval_tool_signal = False
                interval_tool_result = False
                diagnostics.cursor_resets_user_message += 1
                continue

            has_tool, has_result = _tool_signal(top_type, payload)
            if has_tool:
                interval_tool_signal = True
            if has_result:
                interval_tool_result = True

            if ptype != "token_count":
                continue
            diagnostics.token_count_records += 1
            if ts is None:
                diagnostics.missing_timestamp += 1
                continue
            last, total = _usage_objects(payload)
            tokens, next_previous, accepted = _select_usage(last, total, previous_total, diagnostics)
            if not accepted or tokens is None:
                # Intentionally do not move the timing cursor for duplicate,
                # stale, zero-token, or otherwise rejected snapshots.
                previous_total = next_previous
                continue
            previous_total = next_previous

            duration = None
            first_interval = first_after_boundary
            first_after_boundary = False
            if last_accepted_ts is not None:
                delta = (ts - last_accepted_ts).total_seconds()
                if delta > 0:
                    duration = delta
            if duration is None:
                diagnostics.untimed_entries += 1
            entries.append(TimedEntry(
                ts=ts,
                model=current_model or "unknown",
                effort=current_effort or "unknown",
                tokens=tokens,
                duration_seconds=duration,
                first_interval=first_interval,
                tool_signal_interval=interval_tool_signal,
                tool_result_interval=interval_tool_result,
            ))
            diagnostics.accepted_entries += 1

            if last_accepted_ts is None or ts > last_accepted_ts:
                last_accepted_ts = ts
                cursor_source = "token_count"
                interval_tool_signal = False
                interval_tool_result = False

    return entries, diagnostics


def _aggregate_style(entries: Iterable[TimedEntry]) -> dict:
    acc = StyleAccumulator()
    for entry in entries:
        acc.add(entry)
    return acc.finish()


def _difference(cqa: Optional[float], style: Optional[float]) -> dict:
    if cqa is None or style is None:
        return {"absolute_tok_s": None, "percent_vs_cqa": None}
    absolute = style - cqa
    return {
        "absolute_tok_s": absolute,
        "percent_vs_cqa": (absolute / cqa * 100.0) if cqa else None,
    }


def _safe_cqa_stats(stats: Optional[dict]) -> dict:
    stats = stats or {}
    return {
        "visible_output_tokens_per_second": stats.get("visible_output_tokens_per_second"),
        "qualified_visible_responses": int(stats.get("qualified_visible_responses", 0) or 0),
        "visible_responses": int(stats.get("visible_responses", 0) or 0),
        "visible_timed_responses": int(stats.get("visible_timed_responses", 0) or 0),
        "visible_output_tokens": int(stats.get("visible_output_tokens", 0) or 0),
        "visible_generation_seconds": float(stats.get("visible_generation_seconds", 0.0) or 0.0),
        "visible_generation_coverage": stats.get("visible_generation_coverage"),
        "visible_timing_coverage": stats.get("visible_timing_coverage"),
        "generation_quality": stats.get("generation_quality"),
        "qualification_reasons": dict(stats.get("qualification_reasons") or {}),
    }


def _row(scope: str, key: str, cqa_stats: Optional[dict], style_stats: dict,
         tool_excluded_stats: Optional[dict] = None,
         *, model: Optional[str] = None, effort: Optional[str] = None,
         session_ref: Optional[str] = None) -> dict:
    cqa = _safe_cqa_stats(cqa_stats)
    return {
        "scope": scope,
        "key": key,
        "model": model,
        "effort": effort,
        "session_ref": session_ref,
        "cqa_visible": cqa,
        "tokscale_style": style_stats,
        "tool_excluded_response": tool_excluded_stats or _aggregate_tool_excluded([]),
        "difference": _difference(
            cqa.get("visible_output_tokens_per_second"),
            style_stats.get("output_tokens_per_second"),
        ),
    }


def _filter_parsed_for_activation(family: finder.Family,
                                  parsed: Dict[str, lifecycle.ParsedSession],
                                  identities: Mapping[str, dict]) -> Dict[str, lifecycle.ParsedSession]:
    out: Dict[str, lifecycle.ParsedSession] = {}
    for key in family.members:
        src = parsed[key]
        activation = identities.get(key, {}).get("activation")
        dst = lifecycle.ParsedSession()
        if activation is None:
            dst.usage = list(src.usage)
            dst.turn_timings = list(src.turn_timings)
            dst.actions = list(src.actions)
        else:
            dst.usage = [r for r in src.usage if r.ts >= activation]
            dst.turn_timings = [r for r in src.turn_timings if r.ts >= activation]
            dst.actions = [a for a in src.actions if a.ts >= activation]
        dst.action_outputs = src.action_outputs
        dst.parse_errors = src.parse_errors
        dst.duplicate_actions_removed = src.duplicate_actions_removed
        out[key] = dst
    return out


def _discover(home: str, selector: str, recent_days: float) -> Tuple[finder.Family, Dict[str, finder.Session]]:
    discovery = finder.discover_workflow_families(home, finder.DEFAULT_ROLES)
    if selector.lower() == "latest":
        family = finder.latest_workflow_family(
            discovery.families, discovery.sessions, recent_days=recent_days,
            allow_standalone_fallback=True,
        )
    else:
        family = lifecycle.resolve_family(selector, discovery.families, discovery.sessions)
    if family is None:
        raise ValueError("could not uniquely resolve the workflow/session selector against local Codex history")
    return family, discovery.sessions


STAGE_MARKER = ".cqa-tokscale-stage.json"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve_selection(home: str, selector: str, recent_days: float) -> Tuple[str, finder.Family, Dict[str, finder.Session]]:
    resolved_home = os.path.abspath(os.path.expanduser(home))
    family, sessions = _discover(resolved_home, selector, recent_days)
    return resolved_home, family, sessions


def _prepare_stage_root(source_home: str, stage_home: str | Path) -> Path:
    source = Path(source_home).expanduser().resolve()
    target = Path(stage_home).expanduser().resolve()

    # A staging rebuild removes only TARGET/sessions.  Refuse locations that
    # could overlap the real Codex home or its rollout trees.
    if target == source or _is_within(target, source) or _is_within(source, target):
        raise ValueError("Tokscale staging home must be separate from the real Codex home")

    if target.exists() and not target.is_dir():
        raise ValueError("Tokscale staging destination exists and is not a directory")
    target.mkdir(parents=True, exist_ok=True)

    marker = target / STAGE_MARKER
    entries = list(target.iterdir())
    if marker.exists():
        try:
            marker_obj = json.loads(marker.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("Tokscale staging marker is unreadable; refusing to replace existing files") from exc
        if not isinstance(marker_obj, dict) or marker_obj.get("kind") != "cqa-tokscale-stage":
            raise ValueError("Tokscale staging marker is not owned by CQA; refusing to replace existing files")
    elif entries:
        raise ValueError(
            "Tokscale staging destination is not empty and has no CQA staging marker; choose a new directory"
        )

    sessions_dir = target / "sessions"
    if sessions_dir.exists() or sessions_dir.is_symlink():
        if sessions_dir.is_symlink() or not sessions_dir.is_dir():
            sessions_dir.unlink()
        else:
            shutil.rmtree(sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return target


def _stage_relpath(source_home: Path, source_path: Path, ordinal: int) -> Path:
    sessions_root = source_home / "sessions"
    archived_root = source_home / "archived_sessions"
    if _is_within(source_path, sessions_root):
        return source_path.relative_to(sessions_root)
    if _is_within(source_path, archived_root):
        return Path("__archived__") / source_path.relative_to(archived_root)
    # Discovery normally returns only the two trees above.  Keep a deterministic
    # private staging name rather than embedding any source path if that changes.
    return Path("__selected__") / f"session-{ordinal:03d}.jsonl"


def stage_tokscale_home(home: str, family: finder.Family, sessions: Mapping[str, finder.Session],
                        stage_home: str | Path) -> dict:
    """Create a local-only Tokscale home containing exactly FAMILY rollout files.

    Regular files are hard-linked when possible so Tokscale sees ordinary JSONL
    files without duplicating large histories.  Cross-filesystem/permission
    failures fall back to copies.  The stage is intentionally *not* a shareable
    artifact: the rollout files retain their original raw content and filenames.
    """
    source_home = Path(home).expanduser().resolve()
    target = _prepare_stage_root(str(source_home), stage_home)
    sessions_dir = target / "sessions"
    marker_path = target / STAGE_MARKER
    marker_base = {
        "kind": "cqa-tokscale-stage",
        "version": 1,
        "generator": GENERATOR_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "privacy_note": "Local-only raw rollout staging; do not upload or share this directory.",
    }
    # Mark ownership before materialization so an interrupted first build can be
    # safely rebuilt instead of becoming an unowned non-empty directory.
    marker_path.write_text(
        json.dumps({**marker_base, "status": "building"}, indent=2) + "\n",
        encoding="utf-8",
    )

    tz = next((s.first_ts.tzinfo for s in sessions.values() if s.first_ts and s.first_ts.tzinfo), timezone.utc)
    fallback_ts = datetime.min.replace(tzinfo=tz)
    ordered_members = sorted(
        family.members,
        key=lambda k: (sessions[k].first_ts or fallback_ts, k),
    )

    hardlinks = 0
    copies = 0
    for idx, key in enumerate(ordered_members, 1):
        source_path = Path(sessions[key].path).resolve()
        if not source_path.is_file():
            raise ValueError("selected Codex rollout disappeared while building Tokscale staging home")
        rel = _stage_relpath(source_home, source_path, idx)
        dest = sessions_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise ValueError("selected rollout filenames collide inside Tokscale staging home")
        try:
            os.link(source_path, dest)
            hardlinks += 1
        except OSError:
            shutil.copy2(source_path, dest)
            copies += 1

    marker = {
        **marker_base,
        "status": "ready",
        "selected_sessions": len(ordered_members),
        "materialization": {"hardlinks": hardlinks, "copies": copies},
    }
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return {
        "path": target,
        "sessions": len(ordered_members),
        "hardlinks": hardlinks,
        "copies": copies,
    }


def _build_comparison(home: str, family: finder.Family, sessions: Dict[str, finder.Session]) -> dict:
    parsed: Dict[str, lifecycle.ParsedSession] = {}
    for key in family.members:
        parsed[key] = lifecycle.parse_family_session(sessions[key].path, key, finder.DEFAULT_ROLES)
    lifecycle.match_actions(family, sessions, parsed, spawn_window_minutes=30.0, tight_spawn_seconds=2.0)
    identities = attribution.resolve(family, sessions, parsed, finder.DEFAULT_ROLES)
    parsed = _filter_parsed_for_activation(family, parsed, identities)

    tz = next((s.first_ts.tzinfo for s in sessions.values() if s.first_ts and s.first_ts.tzinfo), timezone.utc)
    fallback_ts = datetime.min.replace(tzinfo=tz)
    ordered_members = sorted(
        family.members,
        key=lambda k: (sessions[k].first_ts or fallback_ts, k),
    )
    # Avoid leaking stable hashed session identities in the research artifact;
    # report-local references are sufficient for same-run comparison.
    labels = {key: f"session-{idx:03d}" for idx, key in enumerate(ordered_members, 1)}
    cqa_perf = profiler.build_turn_performance(family, sessions, parsed, labels)

    entries_by_session: Dict[str, List[TimedEntry]] = {}
    tasks_by_session: Dict[str, List[ToolExcludedTask]] = {}
    parse_diag = Counter()
    parse_diag_by_session: Dict[str, dict] = {}
    all_entries: List[TimedEntry] = []
    all_tasks: List[ToolExcludedTask] = []
    for key in ordered_members:
        activation = identities.get(key, {}).get("activation")
        entries, diag = parse_tokscale_style(sessions[key].path, activation=activation)
        tasks = parse_tool_excluded_tasks(sessions[key].path, activation=activation)
        entries_by_session[key] = entries
        tasks_by_session[key] = tasks
        all_entries.extend(entries)
        all_tasks.extend(tasks)
        d = diag.as_dict()
        parse_diag.update(d)
        parse_diag_by_session[labels[key]] = d

    by_model_entries: Dict[str, List[TimedEntry]] = defaultdict(list)
    by_effort_entries: Dict[Tuple[str, str], List[TimedEntry]] = defaultdict(list)
    for entry in all_entries:
        by_model_entries[entry.model or "unknown"].append(entry)
        by_effort_entries[(entry.model or "unknown", entry.effort or "unknown")].append(entry)

    by_model_tasks: Dict[str, List[ToolExcludedTask]] = defaultdict(list)
    by_effort_tasks: Dict[Tuple[str, str], List[ToolExcludedTask]] = defaultdict(list)
    for task in all_tasks:
        by_model_tasks[task.model or "unknown"].append(task)
        by_effort_tasks[(task.model or "unknown", task.effort or "unknown")].append(task)

    rows: List[dict] = []
    rows.append(_row(
        "overall", "overall", cqa_perf.get("overall"), _aggregate_style(all_entries),
        _aggregate_tool_excluded(all_tasks),
    ))

    cqa_by_model = cqa_perf.get("by_model") or {}
    for model in sorted(set(cqa_by_model) | set(by_model_entries) | set(by_model_tasks)):
        rows.append(_row(
            "model", model, cqa_by_model.get(model), _aggregate_style(by_model_entries.get(model, [])),
            _aggregate_tool_excluded(by_model_tasks.get(model, [])),
            model=model,
        ))

    cqa_by_effort = {
        (str(item.get("model", "unknown")), str(item.get("effort", "unknown"))): item.get("stats")
        for item in (cqa_perf.get("by_model_effort") or []) if isinstance(item, dict)
    }
    for model, effort in sorted(set(cqa_by_effort) | set(by_effort_entries) | set(by_effort_tasks)):
        key = f"{model} · {effort}"
        rows.append(_row(
            "model_effort", key, cqa_by_effort.get((model, effort)),
            _aggregate_style(by_effort_entries.get((model, effort), [])),
            _aggregate_tool_excluded(by_effort_tasks.get((model, effort), [])),
            model=model, effort=effort,
        ))

    cqa_by_agent = cqa_perf.get("by_agent") or {}
    for key in ordered_members:
        label = labels[key]
        rows.append(_row(
            "session", label, cqa_by_agent.get(label), _aggregate_style(entries_by_session[key]),
            _aggregate_tool_excluded(tasks_by_session[key]),
            session_ref=label,
        ))

    start = family.first_ts(sessions)
    end = family.last_ts(sessions)
    tokscale_binary = shutil.which("tokscale")
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": "codex-quota-audit", "version": GENERATOR_VERSION},
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "privacy": {
            "profile": "research-safe-v1",
            "contains_prompt_text": False,
            "contains_response_text": False,
            "contains_tool_output": False,
            "contains_file_contents": False,
            "contains_auth_data": False,
            "contains_account_identity": False,
            "contains_source_paths": False,
            "contains_raw_session_ids": False,
        },
        "selection": {
            "workflow_ref": "workflow-001",
            "sessions": len(family.members),
            "observed_range": {
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
        },
        "methods": {
            "cqa_visible_generation": cqa_perf.get("method"),
            "tokscale_style": {
                "status": "research-reconstruction",
                "timing_cursor": "turn_context (or human user_message fallback) -> accepted token_count; intervals are non-overlapping",
                "token_basis": "non-reasoning output tokens from accepted token_count usage snapshots",
                "snapshot_filtering": "duplicate cumulative totals, near-stale regressions, zero-token snapshots, and unusable records do not advance the timing cursor",
                "first_interval": "the first accepted token_count after a turn_context/user_message includes all elapsed client time since that cursor reset",
                "tool_diagnostic": "tool-signal/result interval flags mean tool-related telemetry occurred between timing boundaries; they are not direct measurements of tool runtime",
                "activation_filter": "trusted child spawn time is used to suppress inherited pre-activation fork history when CQA can resolve it",
                "caveat": "This is a Tokscale-style reconstruction, not an upstream Tokscale result. Full Tokscale fork/replay/deduplication behavior is not reimplemented here.",
            },
            "tool_excluded_response": {
                "status": "research",
                "task_interval": "observed task_started event timestamp -> matching task_complete event timestamp",
                "token_basis": "response-level non-reasoning output tokens (output_tokens - reasoning_output_tokens); model-generated tool-call tokens remain model output and are included",
                "tool_exclusion": "subtract the union of model-emitted response-item tool call -> matching response-item tool result/output wall-clock spans, paired by call_id",
                "reasoning_semantics": "reasoning time remains in the denominator; positive timed Reasoning and AgentMessage spans are reported as direct diagnostics but are not required to infer the denominator",
                "residual_semantics": "other_tool_excluded_seconds is the remaining observed client-side time after directly timed Reasoning/AgentMessage spans; it can include TTFT/request latency, inter-item/model-resume latency, tool-call generation/serialization, and small client overhead and is not server-internal compute time",
                "qualification": "headline throughput includes complete tasks with response-level usage and complete valid tool-call/result pairing; incomplete tool pairing is excluded rather than guessed",
                "visible_diagnostic": "exact_visible_output_tokens_per_second is a lower-bound diagnostic using only responses with timed AgentMessage output and no mixed model tool/function-call output",
            },
        },
        "external_validation": {
            "status": "available-not-run" if tokscale_binary else "not-available",
            "tokscale_binary_detected": bool(tokscale_binary),
            "note": (
                "The research command does not execute third-party binaries automatically. "
                "The implementation is covered by non-overlapping-duration fixture tests derived from documented upstream behavior."
            ),
        },
        "diagnostics": {
            "tokscale_style_parser": dict(parse_diag),
            "tokscale_style_parser_by_session": parse_diag_by_session,
        },
        "rows": rows,
    }


def build_comparison(home: str, selector: str, *, recent_days: float = 90.0) -> dict:
    resolved_home, family, sessions = _resolve_selection(home, selector, recent_days)
    return _build_comparison(resolved_home, family, sessions)


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def write_csv(report: Mapping[str, object], path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scope", "key", "model", "effort", "session_ref",
        "cqa_visible_tok_s", "cqa_quality", "cqa_qualified_visible", "cqa_visible_responses",
        "cqa_exact_coverage", "cqa_visible_timing_coverage",
        "tokscale_style_tok_s", "tokscale_style_timed_entries", "tokscale_style_timed_output_tokens",
        "tokscale_style_timed_seconds", "difference_tok_s", "difference_percent_vs_cqa",
        "tokscale_style_later_interval_tok_s", "first_interval_duration_share",
        "tokscale_style_no_tool_signal_tok_s", "tool_signal_duration_share",
        "tokscale_style_no_tool_result_tok_s", "tool_result_duration_share",
        "tool_excluded_response_tok_s", "tool_excluded_exact_visible_tok_s",
        "tool_excluded_quality", "tool_excluded_tasks", "tool_excluded_qualified_tasks",
        "tool_excluded_task_coverage", "tool_excluded_task_elapsed_seconds",
        "tool_excluded_tool_wait_seconds", "tool_excluded_seconds",
        "tool_excluded_timed_reasoning_seconds", "tool_excluded_timed_visible_seconds",
        "tool_excluded_other_seconds", "tool_excluded_tool_pairing_coverage",
        "tool_excluded_reasoning_timing_coverage", "tool_excluded_visible_attribution_coverage",
    ]
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in report.get("rows", []):
            if not isinstance(row, dict):
                continue
            cqa = row.get("cqa_visible") if isinstance(row.get("cqa_visible"), dict) else {}
            style = row.get("tokscale_style") if isinstance(row.get("tokscale_style"), dict) else {}
            diff = row.get("difference") if isinstance(row.get("difference"), dict) else {}
            first = style.get("first_intervals") if isinstance(style.get("first_intervals"), dict) else {}
            later = style.get("later_intervals") if isinstance(style.get("later_intervals"), dict) else {}
            sig = style.get("tool_signal_intervals") if isinstance(style.get("tool_signal_intervals"), dict) else {}
            no_sig = style.get("excluding_tool_signal_intervals") if isinstance(style.get("excluding_tool_signal_intervals"), dict) else {}
            res = style.get("tool_result_intervals") if isinstance(style.get("tool_result_intervals"), dict) else {}
            no_res = style.get("excluding_tool_result_intervals") if isinstance(style.get("excluding_tool_result_intervals"), dict) else {}
            active = row.get("tool_excluded_response") if isinstance(row.get("tool_excluded_response"), dict) else {}
            writer.writerow({
                "scope": row.get("scope"), "key": row.get("key"), "model": row.get("model"),
                "effort": row.get("effort"), "session_ref": row.get("session_ref"),
                "cqa_visible_tok_s": _csv_value(cqa.get("visible_output_tokens_per_second")),
                "cqa_quality": cqa.get("generation_quality"),
                "cqa_qualified_visible": cqa.get("qualified_visible_responses"),
                "cqa_visible_responses": cqa.get("visible_responses"),
                "cqa_exact_coverage": _csv_value(cqa.get("visible_generation_coverage")),
                "cqa_visible_timing_coverage": _csv_value(cqa.get("visible_timing_coverage")),
                "tokscale_style_tok_s": _csv_value(style.get("output_tokens_per_second")),
                "tokscale_style_timed_entries": style.get("timed_entries"),
                "tokscale_style_timed_output_tokens": style.get("timed_output_tokens"),
                "tokscale_style_timed_seconds": _csv_value(style.get("timed_duration_seconds")),
                "difference_tok_s": _csv_value(diff.get("absolute_tok_s")),
                "difference_percent_vs_cqa": _csv_value(diff.get("percent_vs_cqa")),
                "tokscale_style_later_interval_tok_s": _csv_value(later.get("output_tokens_per_second")),
                "first_interval_duration_share": _csv_value(first.get("duration_share")),
                "tokscale_style_no_tool_signal_tok_s": _csv_value(no_sig.get("output_tokens_per_second")),
                "tool_signal_duration_share": _csv_value(sig.get("duration_share")),
                "tokscale_style_no_tool_result_tok_s": _csv_value(no_res.get("output_tokens_per_second")),
                "tool_result_duration_share": _csv_value(res.get("duration_share")),
                "tool_excluded_response_tok_s": _csv_value(active.get("output_tokens_per_second")),
                "tool_excluded_exact_visible_tok_s": _csv_value(active.get("exact_visible_output_tokens_per_second")),
                "tool_excluded_quality": active.get("evidence_quality"),
                "tool_excluded_tasks": active.get("complete_tasks"),
                "tool_excluded_qualified_tasks": active.get("qualified_tasks"),
                "tool_excluded_task_coverage": _csv_value(active.get("task_coverage")),
                "tool_excluded_task_elapsed_seconds": _csv_value(active.get("task_elapsed_seconds")),
                "tool_excluded_tool_wait_seconds": _csv_value(active.get("tool_wait_seconds")),
                "tool_excluded_seconds": _csv_value(active.get("tool_excluded_seconds")),
                "tool_excluded_timed_reasoning_seconds": _csv_value(active.get("timed_reasoning_seconds")),
                "tool_excluded_timed_visible_seconds": _csv_value(active.get("timed_visible_generation_seconds")),
                "tool_excluded_other_seconds": _csv_value(active.get("other_tool_excluded_seconds")),
                "tool_excluded_tool_pairing_coverage": _csv_value(active.get("tool_pairing_coverage")),
                "tool_excluded_reasoning_timing_coverage": _csv_value(active.get("reasoning_timing_coverage")),
                "tool_excluded_visible_attribution_coverage": _csv_value(active.get("visible_attribution_coverage")),
            })
    return target


def write_json(report: Mapping[str, object], path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _default_paths(home: str) -> Tuple[Path, Path]:
    root = Path(os.path.expanduser(home)).resolve() / "codex-quota-audit" / "research"
    return root / "throughput-compare.json", root / "throughput-compare.csv"


def _fmt_rate(value: object) -> str:
    return f"{float(value):.2f}" if isinstance(value, (int, float)) and math.isfinite(float(value)) else "—"


def tokscale_exact_command(stage_path: str | Path) -> str:
    quoted = shlex.quote(str(Path(stage_path)))
    return (
        f"CODEX_HOME={quoted} npx --yes tokscale@latest models "
        "--client codex --group-by model --json > tokscale-exact.json"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="cqa research throughput-compare",
        description=(
            "Compare CQA exact visible-generation tok/s with a Tokscale-style token_count timing reconstruction "
            "over the same local Codex workflow/session."
        ),
    )
    p.add_argument("selector", nargs="?", default="latest", help="W-/S-/raw session selector, or 'latest'")
    p.add_argument("--home", default="~/.codex", help="Codex data directory")
    p.add_argument("--recent-days", type=float, default=90.0, help="lookback used for the 'latest' selector")
    p.add_argument("--json", dest="json_path", metavar="PATH", help="privacy-safe comparison JSON output")
    p.add_argument("--csv", dest="csv_path", metavar="PATH", help="privacy-safe comparison CSV output")
    p.add_argument(
        "--stage-tokscale-home", metavar="PATH",
        help=(
            "create/rebuild a local-only CODEX_HOME containing exactly the selected raw rollout files; "
            "the stage contains private raw telemetry and must not be shared"
        ),
    )
    p.add_argument("--no-json", action="store_true", help="do not write JSON")
    p.add_argument("--no-csv", action="store_true", help="do not write CSV")
    args = p.parse_args(argv)
    if args.recent_days <= 0:
        p.error("--recent-days must be positive")
    if args.no_json and args.no_csv:
        p.error("at least one output format must be enabled")

    default_json, default_csv = _default_paths(args.home)
    json_path = Path(args.json_path).expanduser() if args.json_path else default_json
    csv_path = Path(args.csv_path).expanduser() if args.csv_path else default_csv
    try:
        resolved_home, family, sessions = _resolve_selection(args.home, args.selector, args.recent_days)
        report = _build_comparison(resolved_home, family, sessions)
        stage = (
            stage_tokscale_home(resolved_home, family, sessions, args.stage_tokscale_home)
            if args.stage_tokscale_home else None
        )
    except ValueError as exc:
        print(f"Throughput comparison failed: {exc}", file=os.sys.stderr)
        return 2

    overall = next((r for r in report["rows"] if r.get("scope") == "overall"), None)
    if overall:
        cqa = overall["cqa_visible"].get("visible_output_tokens_per_second")
        style = overall["tokscale_style"].get("output_tokens_per_second")
        active = overall["tool_excluded_response"].get("output_tokens_per_second")
        diff = overall["difference"].get("percent_vs_cqa")
        diff_text = f"{diff:+.1f}%" if isinstance(diff, (int, float)) and math.isfinite(diff) else "—"
        print(f"CQA visible tok/s: {_fmt_rate(cqa)}")
        print(f"Tokscale-style tok/s: {_fmt_rate(style)} ({diff_text} vs CQA)")
        print(f"Tool-excluded response tok/s: {_fmt_rate(active)}")

    if not args.no_json:
        write_json(report, json_path)
        print(f"Comparison JSON: {json_path}")
    if not args.no_csv:
        write_csv(report, csv_path)
        print(f"Comparison CSV: {csv_path}")
    if stage:
        stage_path = Path(stage["path"])
        print(
            f"Tokscale staging home: {stage_path} "
            f"({stage['sessions']} sessions; {stage['hardlinks']} hard links, {stage['copies']} copies)"
        )
        print("Local-only raw telemetry stage: do not upload or share this directory.")
        print("Run upstream Tokscale on the exact selected population with:")
        print(f"  {tokscale_exact_command(stage_path)}")
    print("Note: Tokscale-style is a local reconstruction; it is not an upstream Tokscale measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
