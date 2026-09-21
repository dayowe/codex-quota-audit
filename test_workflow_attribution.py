"""Regression tests for identity, nested accounting and privacy-safe reporting."""
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import find_workflow_candidates as finder
import extract_workflow_lifecycle as lifecycle
import profile_workflow_cost as profiler
import workflow_attribution as identity


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ROLES = finder.DEFAULT_ROLES


def task_label(unit, role, attempt, scope="chunk", run_id="test-run"):
    return identity.encode_label(unit, role, attempt, scope, run_id=run_id)


def fixture():
    sessions = {}
    parsed = {}
    for i, (key, role) in enumerate((("root", "coordinator"), ("lead", "orchestrator"),
                                    ("impl", "implementer"), ("val", "validator"))):
        sessions[key] = finder.Session("/unused", key, first_ts=NOW, last_ts=NOW + timedelta(minutes=10))
        sessions[key].roles.self_role[role] = 2
        parsed[key] = lifecycle.ParsedSession(usage=[lifecycle.UsageRequest(
            NOW + timedelta(minutes=i + 2), key, 100 + i, 50, 10, 4, "unpriced-model", "high")])
    for parent, child, role in (("root", "lead", "orchestrator"), ("lead", "impl", "implementer"),
                                ("lead", "val", "validator")):
        parsed[parent].actions.append(lifecycle.ActionEvent(
            NOW, parent, "spawn", "spawn_agent", "same-call-id-is-caller-local",
            matched_session=child, match_method="spawn-result-id", match_confidence="high",
            task_name=task_label("O-03", role, 1)))
    family = finder.Family(list(sessions), "root", [], "W-fixture")
    labels = {key: f"A{i}" for i, key in enumerate(sessions)}
    return family, sessions, parsed, labels


def analyze(family, sessions, parsed, labels, mapping=None):
    resolved = identity.resolve(family, sessions, parsed, ROLES, mapping)
    return identity.report(family, parsed, resolved, labels, lambda rs: profiler.token_summary(rs, {}))


class IdentityTests(unittest.TestCase):
    def test_versioned_roundtrip_and_no_slug_collisions(self):
        units = ["O-03", "o_03", "o-03", "two/chunks", "é_1"]
        labels = [task_label(u, "implementer", 2) for u in units]
        self.assertEqual(len(labels), len(set(labels)))
        for unit, label in zip(units, labels):
            self.assertRegex(label, r"^[a-z0-9_]+$")
            self.assertEqual(identity.parse_label("/root/lead/" + label, ROLES), identity.Assignment(unit, "implementer", 2, run="test-run"))
        group = task_label("group-one", "orchestrator", 1, "group")
        self.assertEqual(identity.parse_label(group, ROLES).scope, "group")

    def test_legacy_labels_and_rejection(self):
        self.assertEqual(identity.parse_label("O-03:validator:1", ROLES), identity.Assignment("O-03", "validator", 1))
        for label in ["o_03_implementer_1", "please validate O-03", "si1_c_f_validator_1",
                      "si1_61_c_ff_validator_1", "si1_61_c_00_validator_1", "si1_61_c_61_nope_1",
                      "si1_61_c_61_validator_0", "si1_61_c_61_validator_01"]:
            self.assertIsNone(identity.parse_label(label, ROLES), label)

    def test_nested_total_and_no_coordinator_cost_allocation(self):
        f, s, p, labels = fixture()
        result = analyze(f, s, p, labels)
        self.assertEqual(result["total"]["total_tokens"], 446)
        self.assertEqual(result["unattributed"]["total_tokens"], 110)
        self.assertEqual(result["units"][0]["attributed"]["total_tokens"], 336)
        self.assertEqual(sum(r["direct"]["total_tokens"] for r in result["sessions"]), 446)
        self.assertEqual(result["sessions"][0]["inclusive_subtree"]["total_tokens"], 446)
        self.assertEqual(result["sessions"][1]["inclusive_subtree"]["total_tokens"], 336)
        self.assertEqual(result["sessions"][2]["parent"], labels["lead"])
        self.assertEqual(result["sessions"][0]["own_activity_by_active_immediate_child_count"]["1"]["requests"], 1)
        self.assertEqual(result["sessions"][1]["own_activity_by_active_immediate_child_count"]["2"]["requests"], 1)
        self.assertEqual(result["sessions"][0]["direct"]["output_tokens"], 10)  # reasoning not added twice
        self.assertEqual(result["sessions"][0]["direct"]["uncached_input_tokens"], 50)
        self.assertEqual(result["sessions"][0]["direct"]["price_coverage"], 0)
        self.assertNotIn("O-03", json.dumps(result))

    def test_unknown_root_stays_unknown_and_legacy_direct_tree(self):
        f, s, p, labels = fixture()
        s["root"].roles.self_role.clear()
        s["root"].roles.routing_role["orchestrator"] = 8
        self.assertEqual(lifecycle.role_for_key("root", f, s), "unknown/root")
        self.assertEqual(finder.family_role_summary(f, s)["unknown-root"], 1)
        f.root = "lead"; f.members.remove("root")
        result = analyze(f, s, p, labels)
        self.assertEqual(result["total"]["total_tokens"], 336)
        self.assertEqual(result["sessions"][0]["role"], "orchestrator")

    def test_explicit_assignment_preserves_weaker_role_diagnostic(self):
        f, s, p, labels = fixture()
        s["impl"].roles.self_role = Counter({"validator": 2})
        result = analyze(f, s, p, labels)
        row = next(r for r in result["sessions"] if r["session"] == "impl")
        self.assertEqual(row["role"], "implementer")
        self.assertEqual(row["role_confidence"], "trusted-spawn-label")
        self.assertIsNotNone(row["unit"])
        self.assertEqual(row["inferred_role"], "validator")
        self.assertEqual(row["role_diagnostics"], ["inferred-role-disagreement"])
        self.assertFalse(row["issues"])
        self.assertEqual(result["unattributed"]["total_tokens"], 110)

    def test_explicit_self_role_conflict_remains_blocking(self):
        f, s, p, labels = fixture()
        finder.collect_role_evidence({"role": "validator"}, ROLES, "subagent",
                                     s["impl"].roles, "session_meta")
        result = analyze(f, s, p, labels)
        row = next(r for r in result["sessions"] if r["session"] == "impl")
        self.assertEqual(row["role"], "unknown")
        self.assertIsNone(row["unit"])
        self.assertIn("role-conflict", row["issues"])
        self.assertEqual(row["explicit_self_roles"], ["validator"])

    def test_map_label_role_conflict_remains_blocking(self):
        f, s, p, labels = fixture()
        mapping = {"impl": {"parent": "lead", "role": "validator", "assignment": None, "completed_at": None}}
        row = next(r for r in analyze(f, s, p, labels, mapping)["sessions"] if r["session"] == "impl")
        self.assertEqual(row["role"], "unknown")
        self.assertIsNone(row["unit"])
        self.assertIn("role-conflict", row["issues"])

    def test_ancestor_heuristic_disagreement_does_not_block_children(self):
        f, s, p, labels = fixture()
        s["lead"].roles.self_role = Counter({"implementer": 2})
        result = analyze(f, s, p, labels)
        self.assertEqual(result["coverage"]["assigned_or_inherited_sessions"], 3)
        self.assertEqual(result["coverage"]["role_diagnostics"], {"inferred-role-disagreement": 1})
        self.assertEqual(result["unattributed"]["total_tokens"], 110)

    def test_conflicting_spawn_labels_remain_held_out(self):
        f, s, p, labels = fixture()
        p["lead"].actions[0].task_name_conflict = True
        row = next(r for r in analyze(f, s, p, labels)["sessions"] if r["session"] == "impl")
        self.assertIsNone(row["unit"])
        self.assertIn("spawn-label-conflict", row["issues"])

    def test_parent_conflict_and_cycles_are_not_subtrees(self):
        f, s, p, labels = fixture()
        p["root"].actions.append(lifecycle.ActionEvent(NOW, "root", "spawn", "spawn_agent", "other",
            matched_session="impl", match_confidence="high"))
        result = analyze(f, s, p, labels)
        row = next(r for r in result["sessions"] if r["session"] == "impl")
        self.assertIsNone(row["parent"])
        self.assertIn("parent-conflict", row["issues"])
        p["root"].actions.clear()
        p["impl"].actions.append(lifecycle.ActionEvent(NOW, "impl", "spawn", "spawn_agent", "cycle",
            matched_session="lead", match_confidence="high"))
        result = analyze(f, s, p, labels)
        self.assertEqual(result["coverage"]["issues"]["parent-cycle"], 2)

    def test_named_group_is_not_split_and_child_chunk_is_explicit(self):
        f, s, p, labels = fixture()
        p["root"].actions[0].task_name = task_label("F1-group", "orchestrator", 1, "group")
        result = analyze(f, s, p, labels)
        buckets = {r["scope"]: r["attributed"]["total_tokens"] for r in result["units"]}
        self.assertEqual(buckets, {"chunk": 225, "group": 111})

    def test_conflicting_child_chunk_is_not_attributed(self):
        f, s, p, labels = fixture()
        p["lead"].actions[0].task_name = task_label("different", "implementer", 1)
        result = analyze(f, s, p, labels)
        row = next(r for r in result["sessions"] if r["session"] == "impl")
        self.assertIsNone(row["unit"])
        self.assertIn("ancestor-assignment-conflict", row["issues"])

    def test_reused_worker_conflict_and_replacement_attempt(self):
        f, s, p, labels = fixture()
        p["lead"].actions.append(lifecycle.ActionEvent(NOW, "lead", "spawn", "spawn_agent", "second",
            matched_session="impl", match_confidence="high",
            task_name=task_label("other-chunk", "implementer", 2)))
        result = analyze(f, s, p, labels)
        self.assertIn("assignment-conflict-or-reused-session", result["coverage"]["issues"])
        p["lead"].actions.pop()
        s["new"] = finder.Session("/unused", "new")
        p["new"] = lifecycle.ParsedSession()
        labels["new"] = "A4"; f.members.append("new")
        new_action = lifecycle.ActionEvent(NOW, "lead", "spawn", "spawn_agent", "replace",
            matched_session="new", match_confidence="high",
            task_name=task_label("O-03", "implementer", 1))
        p["lead"].actions.append(new_action)
        self.assertIn("assignment-collision", analyze(f, s, p, labels)["coverage"]["issues"])
        new_action.task_name = task_label("O-03", "implementer", 2)
        self.assertNotIn("assignment-collision", analyze(f, s, p, labels)["coverage"]["issues"])

    def test_explicit_mapping_completion_and_family_validation(self):
        f, s, p, labels = fixture()
        p["lead"].actions[0].task_name = "custom_label"
        obj = {"schema": "workflow-assignment-map-v1", "family": "W-fixture", "assignments": [
            {"session": "impl", "parent_session": "lead", "role": "implementer", "unit_id": "O-03",
             "attempt": 1, "run_id": "test-run", "completed_at": (NOW + timedelta(minutes=3)).isoformat()}]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "map.json"; path.write_text(json.dumps(obj))
            mapping = identity.load_mapping(path, f, ROLES)
            result = analyze(f, s, p, labels, mapping)
            row = next(r for r in result["sessions"] if r["session"] == "impl")
            self.assertEqual(row["assignment_source"], "declared-map")
            self.assertEqual(row["activity_after_declared_completion"]["requests"], 1)
            self.assertIsNone(result["sessions"][0]["activity_after_declared_completion"])
            obj["family"] = "wrong-family"; path.write_text(json.dumps(obj))
            with self.assertRaises(ValueError): identity.load_mapping(path, f, ROLES)

    def test_run_namespacing(self):
        f, s, p, labels = fixture()
        resolved = identity.resolve(f, s, p, ROLES)
        def chunk_key(family_key):
            w = profiler.ActiveWindow(0, "implementer", "impl", "A2", NOW, NOW, NOW, "id", "high")
            profiler.apply_assignment_windows([w], resolved, family_key)
            return w.chunk_key
        self.assertNotEqual(chunk_key("W-first"), chunk_key("W-second"))

    def test_same_unit_in_two_explicit_runs_is_not_merged(self):
        f, s, p, labels = fixture()
        s["otherlead"] = finder.Session("/unused", "otherlead")
        p["otherlead"] = lifecycle.ParsedSession(usage=[lifecycle.UsageRequest(NOW, "otherlead", 20, 0, 5, 0, "unknown", "high")])
        labels["otherlead"] = "A5"; f.members.append("otherlead")
        p["root"].actions.append(lifecycle.ActionEvent(NOW, "root", "spawn", "spawn_agent", "other-run",
            matched_session="otherlead", match_confidence="high",
            task_name=task_label("O-03", "orchestrator", 1, run_id="another-run")))
        result = analyze(f, s, p, labels)
        self.assertEqual(len(result["units"]), 2)
        self.assertEqual(len({u["run"] for u in result["units"]}), 2)
        self.assertEqual(sum(u["attributed"]["total_tokens"] for u in result["units"]) + result["unattributed"]["total_tokens"], 471)

    def test_map_and_observed_parent_conflict_is_held(self):
        f, s, p, labels = fixture()
        mapping = {"impl": {"parent": "root", "role": "implementer", "assignment": None, "completed_at": None}}
        row = next(r for r in analyze(f, s, p, labels, mapping)["sessions"] if r["session"] == "impl")
        self.assertIn("parent-conflict", row["issues"])
        self.assertIsNone(row["unit"])


class FamilySelectorTests(unittest.TestCase):
    def test_raw_session_id_metadata_and_existing_selectors(self):
        f, s, _, _ = fixture()
        raw = "agent_coordinator123456789"
        s["root"].links.own_ids.add(finder.fp(raw))
        self.assertIs(lifecycle.resolve_family(raw, [f], s), f)
        self.assertIs(lifecycle.resolve_family("  " + raw + "  ", [f], s), f)
        self.assertIs(lifecycle.resolve_family(f.family_key, [f], s), f)
        self.assertIsNone(lifecycle.resolve_family("missing_session123456", [f], s))
        self.assertIsNone(lifecycle.resolve_family("/path/to/a/session", [f], s))

    def test_rollout_identity_and_hashed_member_selectors(self):
        raw = "01234567-1234-5678-abcd-0123456789ab"
        key = finder.short_key("S", finder.fp(raw))
        sessions = {key: finder.Session("/unused", key)}
        f = finder.Family([key], key, [], "W-test")
        self.assertIs(lifecycle.resolve_family(raw, [f], sessions), f)
        self.assertIs(lifecycle.resolve_family(key, [f], sessions), f)

    def test_ambiguous_metadata_is_not_guessed(self):
        raw = "ambiguous_session123456"
        sessions = {key: finder.Session("/unused", key) for key in ("a", "b")}
        for s in sessions.values():
            s.links.own_ids.add(finder.fp(raw))
        families = [finder.Family([key], key, [], "W-" + key) for key in sessions]
        self.assertIsNone(lifecycle.resolve_family(raw, families, sessions))


class RoleEvidenceTests(unittest.TestCase):
    def test_nested_path_uses_only_own_leaf_and_ignores_sender(self):
        leaf = task_label("O-03", "validator", 1)
        parent = "/root/" + task_label("O-03", "orchestrator", 1)
        own = parent + "/" + leaf
        ev = finder.RoleEvidence()
        finder.collect_role_evidence({"agent_path": own, "source": {"subagent": {
            "thread_spawn": {"agent_path": own}}}}, ROLES, "subagent", ev, "session_meta")
        for _ in range(5):
            finder.collect_role_evidence({"item": {"agent_path": parent, "role": "orchestrator"}},
                                         ROLES, "subagent", ev, "event_msg")
        self.assertEqual(ev.self_role, Counter({"validator": 2}))
        self.assertFalse(ev.explicit_self_role)  # naming is not an explicit role field
        self.assertEqual(ev.routing_role["orchestrator"], 5)

    def test_routing_prose_and_other_session_metadata_are_not_self(self):
        ev = finder.RoleEvidence()
        payload = {"recipient": "/root/validator", "item": {"role": "implementer"},
                   "state": {"environments": {"subagents": [{"role": "validator"}]}},
                   "message": "coordinator orchestrator", "source": {"subagent": {
                       "thread_spawn": {"parent_agent_path": "/root/orchestrator"}}}}
        finder.collect_role_evidence(payload, ROLES, "subagent", ev, "session_meta")
        self.assertFalse(ev.self_role)
        self.assertFalse(ev.explicit_self_role)

    def test_generic_leaf_remains_inference_without_fabricated_assignment(self):
        ev = finder.RoleEvidence()
        finder.collect_role_evidence({"agent_path": "/root/orchestrator/custom_validator"},
                                     ROLES, "subagent", ev, "session_meta")
        self.assertEqual(ev.self_role, Counter({"validator": 1}))
        self.assertIsNone(identity.parse_label("/root/orchestrator/custom_validator", ROLES))


class ToolTests(unittest.TestCase):
    def test_result_side_label_and_equivalent_full_task_path(self):
        label = task_label("O-03", "implementer", 1)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            for argument_name in (None, label):
                args = {"task_name": argument_name} if argument_name else {}
                payloads = [
                    {"type": "function_call", "name": "spawn_agent", "call_id": "spawn-123456789", "arguments": json.dumps(args)},
                    {"type": "function_call_output", "call_id": "spawn-123456789", "output": json.dumps({"agent_id": "agent_child123456", "task_name": "/root/" + label})},
                ]
                path.write_text("\n".join(json.dumps({"timestamp": NOW.isoformat(), "payload": p}) for p in payloads))
                action = lifecycle.parse_family_session(str(path), "S-test", ROLES).actions[0]
                self.assertEqual(action.task_name, label)
                self.assertFalse(action.task_name_conflict)

    def test_custom_wrappers_duplicates_and_fake_embedded_calls(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            payloads = [
                {"type": "custom_tool_call", "name": "functions.exec", "call_id": "wrapper", "input": "await tools.exec_command({cmd:'cat secret'})"},
                {"type": "function_call", "name": "functions.exec_command", "call_id": "read", "arguments": '{"cmd":"cat secret.txt"}'},
                {"type": "function_call", "name": "functions.exec_command", "call_id": "read", "arguments": '{"cmd":"cat secret.txt"}'},
                {"type": "function_call_output", "call_id": "fake", "output": {"name": "exec_command", "arguments": {"cmd": "cat no.txt"}}},
                {"type": "function_call", "name": "collaboration.interrupt_agent", "call_id": "stop", "arguments": '{"target":"worker"}'},
            ]
            path.write_text("\n".join(json.dumps({"timestamp": NOW.isoformat(), "type": "response_item", "payload": p}) for p in payloads))
            scanned = profiler.scan_raw_compaction_session(str(path), "S-test", None, None)
            self.assertEqual(Counter(t.category for t in scanned.tools), {"opaque-wrapper": 1, "file-read": 1, "lifecycle": 1})
            self.assertEqual(scanned.tool_duplicates, 1)
            self.assertFalse(scanned.tools[0].resource_hashes)
            self.assertEqual(lifecycle.parse_family_session(str(path), "S-test", ROLES).actions[0].kind, "interrupt")


class EndToEndTests(unittest.TestCase):
    def test_cli_nested_tree_replay_and_privacy(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); logdir = home / "sessions"; logdir.mkdir()
            records = {}
            roles = {"root": "coordinator", "lead": "orchestrator", "impl": "implementer", "val": "validator"}
            parents = {"lead": "root", "impl": "lead", "val": "lead"}
            ids = {key: f"agent_{key}123456789" for key in roles}

            def rec(second, payload, kind="response_item"):
                return {"timestamp": (NOW + timedelta(seconds=second)).isoformat(), "type": kind, "payload": payload}

            def usage(second, total, amount=110):
                return rec(second, {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": amount - 10, "cached_input_tokens": 50, "output_tokens": 10, "reasoning_output_tokens": 4},
                    "total_token_usage": {"total_tokens": total}}}, "event_msg")

            for i, (key, role) in enumerate(roles.items()):
                payload = {"session_id": ids[key], "role": role, "model": "unpriced-model", "source": "cli" if key == "root" else "subagent"}
                if key in ("impl", "val"):
                    payload.pop("role")
                    own = "/root/" + task_label("private-unit", "orchestrator", 1) + "/" + task_label("private-unit", role, 1)
                    payload["agent_path"] = own
                    payload["source"] = {"subagent": {"thread_spawn": {"agent_path": own}}}
                if key in parents:
                    payload["parent_session_id"] = ids[parents[key]]
                records[key] = [rec(i * 10, payload, "session_meta")]
                if key in ("impl", "val"):
                    records[key].append(rec(40, {"item": {"agent_path": "/root/orchestrator"}}, "event_msg"))
                if key == "impl":
                    records[key].append(usage(1, 90, 90))  # inherited pre-spawn history
                records[key].extend([usage(60 + i, 110), usage(60 + i, 110)])  # duplicate snapshot
            for i, child in enumerate(("lead", "impl", "val"), 1):
                parent = parents[child]
                call_id = f"call-{i}"
                records[parent].append(rec(i * 10, {"type": "function_call", "name": "collaboration.spawn_agent",
                    "call_id": call_id, "arguments": json.dumps({"task_name": task_label("private-unit", roles[child], 1),
                                                                  "message": "PRIVATE_PROMPT_DO_NOT_EXPORT"})}))
                records[parent].append(rec(i * 10 + 0.1, {"type": "function_call_output", "call_id": call_id,
                                                                        "output": json.dumps({"agent_id": ids[child]})}))
            for key, rows in records.items():
                (logdir / f"{key}.jsonl").write_text("\n".join(json.dumps(r) for r in sorted(rows, key=lambda r: r["timestamp"])))
            sessions = [finder.scan_session(str(p), ROLES) for p in logdir.glob("*.jsonl")]
            root = next(s.session_key for s in sessions if s.inferred_role[0] == "coordinator")
            out = home / "report.json"
            command = [sys.executable, str(Path(profiler.__file__)), "--home", str(home), "--session", ids["root"],
                       "--export-json", str(out), "--no-action-schema-audit"]
            process = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr[-2000:])
            report = json.loads(out.read_text())
            nested = report["nested_attribution"]
            self.assertEqual(report["root_role"], "coordinator")
            self.assertEqual(nested["total"]["total_tokens"], 440)
            self.assertEqual(nested["pre_activation_records_excluded"]["requests"], 1)
            self.assertEqual(nested["unattributed"]["total_tokens"], 110)
            self.assertEqual(nested["units"][0]["attributed"]["total_tokens"], 330)
            self.assertEqual(nested["coverage"]["explicit_assignments"], 3)
            self.assertFalse(nested["coverage"]["issues"])
            self.assertEqual({r["role"] for r in nested["sessions"]}, set(roles.values()))
            exported = out.read_text() + process.stdout
            for forbidden in ["private-unit", "PRIVATE_PROMPT_DO_NOT_EXPORT", *ids.values()]:
                self.assertNotIn(forbidden, exported)
            lifecycle_out = home / "lifecycle.json"
            extract = subprocess.run([sys.executable, str(Path(lifecycle.__file__)), "--home", str(home),
                                      "--session-id", ids["root"], "--export-json", str(lifecycle_out)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(extract.returncode, 0, extract.stderr[-2000:])
            self.assertNotIn(ids["root"], extract.stdout + lifecycle_out.read_text())
            # A cutoff must not lose the coordinator or its nested family.
            process = subprocess.run(command + ["--after", (NOW + timedelta(seconds=40)).isoformat(),
                                               "--analysis-root", root], capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr[-2000:])
            windowed = json.loads(out.read_text())
            self.assertEqual(windowed["root_role"], "coordinator")
            self.assertEqual(windowed["nested_attribution"]["total"]["total_tokens"], 110)  # carry-in children remain separate
            process = subprocess.run(command + ["--after", NOW.isoformat(), "--before", (NOW + timedelta(seconds=63)).isoformat()],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr[-2000:])
            bounded = json.loads(out.read_text())
            self.assertEqual(bounded["root_role"], "coordinator")  # auto selection, no override
            self.assertEqual(bounded["nested_attribution"]["total"]["total_tokens"], 330)


if __name__ == "__main__":
    unittest.main()
