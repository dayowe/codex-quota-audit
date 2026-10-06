"""Extract reusable, content-free workflow evidence in one streaming pass."""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass, field

from . import candidates as finder
from . import lifecycle, records


@dataclass
class Telemetry:
    parsed: lifecycle.ParsedSession
    compaction: object
    arguments: list = field(default_factory=list)
    results: list = field(default_factory=list)
    role_targets: list = field(default_factory=list)
    response_records: list = field(default_factory=list)


def _response_record(obj, payload):
    """Whitelist timing/count fields; omit all messages and tool content."""
    top = str(obj.get("type", ""))
    kind = str(payload.get("type", ""))
    model, effort = finder.model_from_payload(payload), finder.effort_from_payload(payload)
    clean = {"type": kind}
    if model:
        clean["model"] = model
    if effort:
        clean["reasoning_effort"] = effort
    turn = payload.get("turn_id")
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(turn, str) and isinstance(metadata, dict):
        turn = metadata.get("turn_id")
    if isinstance(turn, str) and turn:
        clean["turn_id"] = hashlib.sha256(turn.encode()).hexdigest()
    call = payload.get("call_id")
    if isinstance(call, str) and call:
        clean["call_id"] = hashlib.sha256(call.encode()).hexdigest()
    for name in ("started_at_ms", "completed_at_ms", "time_to_first_token_ms"):
        value = payload.get(name)
        if isinstance(value, (int, float)):
            clean[name] = value
    item = payload.get("item")
    if isinstance(item, dict):
        clean["item"] = {"type": str(item.get("type", ""))}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        clean["usage"] = {k: int(v or 0) for k, v in usage.items() if k in {
            "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"
        }}
    return {"timestamp": obj.get("timestamp"), "type": top, "payload": clean}


def extract(path, key, roles):
    from . import profile
    consumers = [lifecycle.session_consumer(key, roles),
                 profile.compaction_consumer(key, None, None, keep_duplicates=True)]
    out = Telemetry(lifecycle.ParsedSession(), profile.RawCompactionSession())
    try:
        for consumer in consumers:
            next(consumer)
        with open(path, "rb") as stream:
            for line in stream:
                raw = records.Record(line)
                for consumer in consumers:
                    consumer.send(raw)
                low = raw.lower()
                # Only structural action/result and response timing records need
                # the additional evidence extraction. Legacy compacted histories
                # are handled by the compaction consumer without decoding here.
                if b'"type": "compacted"' in raw[:4096] or b'"type":"compacted"' in raw[:4096]:
                    continue
                timing_hint = records.response_timing_hint(raw)
                if not timing_hint and not any(x in low for x in (b'call', b'agent', b'wait')):
                    continue
                try:
                    obj = records.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                ts = finder.parse_ts(obj.get("timestamp"))
                if ts is None or not isinstance(payload, dict):
                    continue
                response = _response_record(obj, payload) if timing_hint else None
                if response:
                    out.response_records.append(response)
                args_hint = any(x in low for x in (b'spawn_agent', b'send_input', b'wait', b'resume_agent', b'close_agent', b'create_agent'))
                role_hint = any(x in low for x in (b'send_input', b'send_message', b'message_agent'))
                if args_hint or role_hint:
                    for node in lifecycle.action_node_candidates(payload):
                        _, kind, args, call_raw = profile.action_name_and_args(node)
                        if not kind:
                            continue
                        call = lifecycle.hash_id(call_raw) if call_raw is not None else None
                        if args_hint:
                            out.arguments.append((ts, kind, call, list(profile.schema_walk(args, roles))))
                        if kind == "send" and role_hint and call:
                            role = profile.exact_role_target_from_args(args, roles)
                            if role:
                                out.role_targets.append((ts, call, role))
                if any(x in raw for x in (b'"call_id"', b'"tool_call_id"', b'"function_call_id"')):
                    rec = profile.output_record_raw(payload)
                    if rec:
                        call, result = rec
                        out.results.append((ts, call, list(profile.schema_walk(result, roles))))
        out.parsed, out.compaction = [records.finish(c) for c in consumers]
    finally:
        for consumer in consumers:
            consumer.close()
    return out


def compaction_view(observation, start, end):
    from . import profile
    src = observation.compaction
    out = profile.RawCompactionSession(parse_errors=src.parse_errors)
    out.markers = [x for x in src.markers if profile.in_time_window(x.ts, start, end)]
    out.usage = [x for x in src.usage if profile.in_time_window(x.ts, start, end)]
    seen = set()
    for identity, event in src.tool_records:
        if not profile.in_time_window(event.ts, start, end):
            continue
        if identity in seen:
            out.tool_duplicates += 1
        else:
            seen.add(identity)
            out.tools.append(event)
    out.tools.sort(key=lambda t: t.ts)
    return out


def schema_audit(family, sessions, parsed, roles, start, end, observations):
    from . import profile
    out = profile.SchemaAudit()
    family_ids = set().union(*(sessions[k].links.own_ids for k in family.members))
    actions = {a.call_id: a for k in family.members for a in parsed[k].actions if a.call_id}
    for key in family.members:
        local_index = 0
        for ts, kind, call, fields in observations[key].arguments:
            if not profile.in_time_window(ts, start, end):
                continue
            local_index += 1
            cid = call or f"local:{key}:{local_index}"
            out.call_totals[(kind, "args")] += 1
            seen = set()
            for path, typ, scalar, idshape, rolelike in fields:
                stat = profile.schema_stat(out, (kind, "args", path))
                stat.occurrences += 1
                stat.types[typ] += 1
                if path not in seen:
                    stat.calls_present.add(cid); seen.add(path)
                if scalar:
                    stat.scalar_hashes.add(scalar)
                    if call:
                        out.action_arg_tokens[call].add(scalar)
                    stat.family_id_matches += int(scalar in family_ids)
                stat.id_shaped += int(idshape)
                stat.role_label_hits += int(rolelike)
    for key in family.members:
        for ts, call, fields in observations[key].results:
            action = actions.get(call)
            if action is None or not profile.in_time_window(ts, start, end):
                continue
            out.call_totals[(action.kind, "result")] += 1
            seen, tokens = set(), set()
            for path, typ, scalar, idshape, rolelike in fields:
                stat = profile.schema_stat(out, (action.kind, "result", path))
                stat.occurrences += 1
                stat.types[typ] += 1
                if path not in seen:
                    stat.calls_present.add(call); seen.add(path)
                if scalar:
                    stat.scalar_hashes.add(scalar); tokens.add(scalar)
                    stat.family_id_matches += int(scalar in family_ids)
                stat.id_shaped += int(idshape)
                stat.role_label_hits += int(rolelike)
            if action.kind == "spawn" and lifecycle.is_trusted_action(action) and action.matched_session:
                for token in tokens:
                    out.spawn_output_token_to_children[token].add(action.matched_session)
    for action in (a for key in family.members for a in parsed[key].actions):
        if action.kind not in {"send", "wait", "resume", "close"} or lifecycle.is_trusted_action(action):
            continue
        children = set()
        for token in out.action_arg_tokens.get(action.call_id, set()):
            matched = out.spawn_output_token_to_children.get(token, set())
            if len(matched) == 1:
                children |= matched
        if len(children) == 1:
            out.bridge_candidates[f"{action.kind}_unique_spawn_handle"] += 1
        elif children:
            out.bridge_candidates[f"{action.kind}_ambiguous_spawn_handle"] += 1
    spawn_tokens = set(out.spawn_output_token_to_children)
    for (kind, side, path), stat in out.stats.items():
        if side == "args" and kind in {"send", "wait", "resume", "close"}:
            stat.spawn_handle_overlap = len(stat.scalar_hashes & spawn_tokens)
    return out
