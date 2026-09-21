"""Explicit assignment identity and nested accounting; no transcript classification.

Raw labels/map values stay local. Reports contain hashed session keys and report-local
unit labels only. The mapping is optional audit input, not a new agent journal.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import extract_workflow_lifecycle as lifecycle


@dataclass(frozen=True)
class Assignment:
    unit: str
    role: str
    attempt: int
    scope: str = "chunk"
    run: Optional[str] = None


def parse_label(value, roles) -> Optional[Assignment]:
    """si1_<hex run>_<c|g>_<hex unit>_<role>_<positive attempt>, or legacy colons.

    Hex preserves case and punctuation and cannot collide through slugification.
    Full tool task paths are allowed only for the explicitly versioned format.
    """
    if not isinstance(value, str) or len(value) > 512 or any(c.isspace() for c in value):
        return None
    leaf = value.rsplit("/", 1)[-1]
    m = re.fullmatch(r"si1_([0-9a-f]+)_([cg])_([0-9a-f]+)_([a-z]+)_([1-9][0-9]*)", leaf)
    if m:
        encoded_run, scope, encoded, role, attempt = m.groups()
        try:
            unit = bytes.fromhex(encoded).decode("utf-8")
            run = bytes.fromhex(encoded_run).decode("utf-8")
        except (ValueError, UnicodeError):
            return None
        if role not in roles or any(not text or len(text.encode("utf-8")) > 96 or any(ord(c) < 32 for c in text) for text in (unit, run)):
            return None
        return Assignment(unit, role, int(attempt), "chunk" if scope == "c" else "group", run)
    m = re.fullmatch(r"([A-Za-z0-9._/-]{1,96}):([a-z]+):([1-9][0-9]*)", value)
    if m and m[2] in roles:
        return Assignment(m[1], m[2], int(m[3]))
    return None


def encode_label(unit, role, attempt, scope="chunk", *, run_id) -> str:
    if scope not in {"chunk", "group"} or type(attempt) is not int or attempt < 1:
        raise ValueError("Invalid assignment scope or attempt")
    label = f"si1_{run_id.encode('utf-8').hex()}_{'c' if scope == 'chunk' else 'g'}_{unit.encode('utf-8').hex()}_{role}_{attempt}"
    if parse_label(label, {role}) != Assignment(unit, role, attempt, scope, run_id):
        raise ValueError("Invalid assignment label")
    return label


def load_mapping(path, family, roles) -> dict:
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        raise ValueError("Cannot read assignment map JSON") from None
    if not isinstance(obj, dict) or obj.get("schema") != "workflow-assignment-map-v1" or obj.get("family") != family.family_key:
        raise ValueError("Assignment map schema/family mismatch")
    if not isinstance(obj.get("assignments"), list):
        raise ValueError("Assignment map requires an assignments list")
    out = {}
    for row in obj["assignments"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid assignment map entry")
        key, parent = row.get("session"), row.get("parent_session")
        if key not in family.members or key in out or (parent is not None and parent not in family.members) or parent == key:
            raise ValueError("Unknown, duplicate or self-parented assignment map entry")
        if key == family.root and parent is not None:
            raise ValueError("Analysis root cannot have a mapped parent")
        role = row.get("role")
        if role not in roles:
            raise ValueError("Unrecognized assignment-map role")
        assignment = None
        if "unit_id" in row:
            try:
                label = encode_label(row["unit_id"], role, row["attempt"], row.get("scope", "chunk"), run_id=row["run_id"])
                assignment = parse_label(label, roles)
            except (KeyError, TypeError, AttributeError, ValueError):
                raise ValueError("Invalid mapped assignment identity") from None
        completed = None
        if row.get("completed_at") is not None:
            try:
                completed = datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00"))
                if completed.utcoffset() is None:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Completion timestamp must include a timezone") from None
        out[key] = {"parent": parent, "role": role, "assignment": assignment, "completed_at": completed}
    return out


def resolve(family, sessions, parsed, roles, mapping=None):
    """Resolve immediate parents and assignments; contradictory evidence is held out.

    Graph ancestry is not necessarily an immediate parent. Trusted spawn callers
    take precedence; only high-confidence explicit parent edges are fallback.
    """
    mapping = mapping or {}
    parents, assignments = defaultdict(set), defaultdict(set)
    activations = defaultdict(list)
    label_conflicts = set()
    for key in family.members:
        for action in parsed[key].actions:
            if action.kind == "spawn" and lifecycle.is_trusted_action(action) and action.matched_session in family.members:
                child = action.matched_session
                parents[child].add(action.caller_key)
                activations[child].append(action.ts)
                assignment = parse_label(action.task_name, roles)
                if action.task_name_conflict:
                    label_conflicts.add(child)
                if assignment:
                    assignments[child].add(assignment)
    rows = {}
    for key in family.members:
        role, confidence = sessions[key].inferred_role
        inferred_role, inferred_confidence = role, confidence
        diagnostics = []
        if sessions[key].dominant_model == "codex-auto-review":
            role, confidence = "guardian/auto-review", "model-metadata"
        issues = ["spawn-label-conflict"] if key in label_conflicts else []
        candidates = parents[key]
        parent_source = "trusted-spawn"
        if not candidates:
            candidates = {e.parent for e in family.edges if e.child == key and e.parent in family.members
                          and e.confidence == "high" and e.method == "explicit-parent-id"}
            parent_source = "explicit-parent-id"
        mapped = mapping.get(key)
        if mapped and mapped["parent"] is not None:
            if candidates and mapped["parent"] not in candidates:
                issues.append("parent-conflict")
            elif not candidates:
                candidates = {mapped["parent"]}
                parent_source = "declared-map"
        if len(candidates) > 1 or key in candidates or (key == family.root and candidates):
            issues.append("parent-conflict")
        parent = next(iter(candidates)) if len(candidates) == 1 and "parent-conflict" not in issues else None
        identities = set(assignments[key])
        if mapped and mapped["assignment"]:
            ma = mapped["assignment"]
            identities = {a for a in identities if not (a.run is None and
                          (a.unit, a.role, a.attempt, a.scope) == (ma.unit, ma.role, ma.attempt, ma.scope))}
            identities.add(ma)
        if len(identities) > 1:
            issues.append("assignment-conflict-or-reused-session")
        assignment = next(iter(identities)) if len(identities) == 1 else None
        declared_roles = {a.role for a in identities}
        if mapped:
            declared_roles.add(mapped["role"])
        # Naming/routing heuristics cannot erase an explicit assignment. Exact
        # self-role declarations are different: conflicting explicit sources
        # must remain unresolved, even when one is a trusted spawn label.
        explicit_self_roles = set(sessions[key].roles.explicit_self_role)
        declared_roles.update(explicit_self_roles)
        if sessions[key].dominant_model == "codex-auto-review":
            declared_roles.add("guardian/auto-review")
        if len(declared_roles) > 1:
            issues.append("role-conflict")
            role, confidence, assignment = "unknown", "conflict", None
        elif declared_roles:
            role = next(iter(declared_roles))
            confidence = ("declared-map" if mapped else "trusted-spawn-label" if assignment
                          else "explicit-self-metadata" if explicit_self_roles else "model-metadata")
        if declared_roles and inferred_role != "unknown" and inferred_role != role:
            diagnostics.append("inferred-role-disagreement")
        rows[key] = {
            "parent": parent, "parent_source": parent_source if parent else None,
            "role": role, "role_confidence": confidence, "assignment": assignment,
            "inferred_role": inferred_role, "inferred_role_confidence": inferred_confidence,
            "explicit_self_roles": sorted(explicit_self_roles), "role_diagnostics": diagnostics,
            "assignment_source": ("declared-map" if mapped and mapped["assignment"] else "trusted-spawn-label") if assignment else None,
            "activation": min(activations[key]) if activations[key] else None,
            "completed_at": mapped["completed_at"] if mapped else None, "issues": issues,
        }
    # Reject every member of a cycle, without turning the chosen root into proof.
    for key in rows:
        path = []
        current = key
        while current is not None and current not in path:
            path.append(current)
            current = rows[current]["parent"]
        if current is not None:
            for member in path[path.index(current):]:
                rows[member]["issues"].append("parent-cycle")
    for row in rows.values():
        if "parent-cycle" in row["issues"]:
            row["parent"] = None
            row["parent_source"] = None
    owners = defaultdict(list)
    for key, row in rows.items():
        if row["assignment"]:
            owners[(row["parent"], row["assignment"])].append(key)
        if row["completed_at"] and row["activation"] and row["completed_at"] < row["activation"]:
            row["issues"].append("completion-before-activation")
            row["completed_at"] = None
    for members in owners.values():
        if len(members) > 1:
            for key in members:
                rows[key]["issues"].append("assignment-collision")
    return rows


def report(family, parsed, identities, labels, summarize):
    """Direct totals partition requests; inclusive subtrees and time exposure do not.

    summarize consumes UsageRequests and returns a JSON-safe token summary.
    """
    children = defaultdict(list)
    for key, row in identities.items():
        if row["parent"]:
            children[row["parent"]].append(key)

    def descendants(key):
        result, todo = {key}, list(children[key])
        while todo:
            child = todo.pop()
            if child not in result:
                result.add(child)
                todo.extend(children[child])
        return result

    def nearest_assignment(key):
        row = identities[key]
        if row["issues"]:
            return None, None
        if row["assignment"]:
            # A chunk cannot silently acquire a different child chunk. A named
            # group may contain explicit chunks, which remain distinct buckets.
            ancestor = row["parent"]
            while ancestor:
                owner = identities[ancestor]
                if owner["issues"]:
                    return None, None
                a = owner["assignment"]
                if a and ((a.run is not None and row["assignment"].run is not None and a.run != row["assignment"].run) or
                          (a.scope == "chunk" and (a.scope, a.unit) != (row["assignment"].scope, row["assignment"].unit))):
                    row["issues"].append("ancestor-assignment-conflict")
                    return None, None
                ancestor = owner["parent"]
            return row["assignment"], key
        return nearest_assignment(row["parent"]) if row["parent"] else (None, None)

    ownership = {key: nearest_assignment(key) for key in identities}
    def unit_key(a):
        return (a.run or "", a.scope, a.unit)

    units = sorted({unit_key(a) for a, _ in ownership.values() if a})
    unit_labels = {unit: f"U{i:02d}" for i, unit in enumerate(units, 1)}
    runs = {run: f"R{i:02d}" for i, run in enumerate(sorted({u[0] for u in units if u[0]}), 1)}
    rows, unit_requests, unattributed = [], defaultdict(list), []
    for key in family.members:
        row = identities[key]
        reqs = parsed[key].usage
        assignment, owner = ownership[key]
        unit = unit_key(assignment) if assignment else None
        if unit:
            unit_requests[unit].extend(reqs)
        else:
            unattributed.extend(reqs)
        members = descendants(key)
        counts = Counter()
        targets = Counter()
        for action in parsed[key].actions:
            counts[action.kind] += 1
            targets[lifecycle.attribution_class(action)] += 1
        child_windows = []
        for child in children[key]:
            start = identities[child]["activation"]
            events = [r.ts for r in parsed[child].usage] + [a.ts for a in parsed[child].actions]
            if start is not None and events:
                child_windows.append((start, max(events)))
        exposures = defaultdict(list)
        for req in reqs:
            n = sum(start <= req.ts <= end for start, end in child_windows)
            exposures[str(n)].append(req)
        completed = row["completed_at"]
        rows.append({
            "agent": labels[key], "session": key, "is_root": key == family.root,
            "role": row["role"], "role_confidence": row["role_confidence"],
            "inferred_role": row["inferred_role"], "inferred_role_confidence": row["inferred_role_confidence"],
            "explicit_self_roles": row["explicit_self_roles"], "role_diagnostics": row["role_diagnostics"],
            "parent": labels.get(row["parent"]), "parent_source": row["parent_source"],
            "unit": unit_labels.get(unit), "assignment_owner": labels.get(owner),
            "assignment_source": identities[owner]["assignment_source"] if owner else None,
            "attempt": row["assignment"].attempt if row["assignment"] else None,
            "direct": summarize(reqs),
            "inclusive_subtree": summarize(r for member in members for r in parsed[member].usage),
            "lifecycle_actions": dict(counts), "action_target_coverage": dict(targets),
            "own_activity_by_active_immediate_child_count": {n: summarize(rs) for n, rs in sorted(exposures.items())},
            "activity_after_declared_completion": summarize(r for r in reqs if r.ts > completed) if completed else None,
            "issues": sorted(set(row["issues"])),
        })
    return {
        "note": "Direct session totals and unit buckets are additive partitions. Inclusive subtrees overlap; do not sum them. Child-window exposure is temporal, not causal cost attribution. Session lifetime is not continuous inference.",
        "sessions": rows,
        "units": [{"unit": unit_labels[u], "run": runs.get(u[0]), "scope": u[1], "attributed": summarize(unit_requests[u]),
                   "by_role": {role: summarize(r for key in family.members
                       if identities[key]["role"] == role and ownership[key][0] and
                       unit_key(ownership[key][0]) == u for r in parsed[key].usage)
                       for role in sorted({row["role"] for row in identities.values()})}}
                  for u in units],
        "unattributed": summarize(unattributed),
        "total": summarize(r for key in family.members for r in parsed[key].usage),
        "coverage": {
            "sessions": len(rows), "explicit_assignments": sum(r["assignment"] is not None for r in identities.values()),
            "assigned_or_inherited_sessions": sum(a is not None for a, _ in ownership.values()),
            "unresolved_nonroot_parents": sum(k != family.root and r["parent"] is None for k, r in identities.items()),
            "issues": dict(Counter(issue for r in rows for issue in r["issues"])),
            "role_diagnostics": dict(Counter(d for r in rows for d in r["role_diagnostics"])),
        },
    }
