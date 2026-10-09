"""Portable workflow fixtures: role-independent accounting and explicit limits."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SUBPROCESS_ENV = os.environ.copy()
SUBPROCESS_ENV["PYTHONPATH"] = str(SRC) + os.pathsep + SUBPROCESS_ENV.get("PYTHONPATH", "")

from cqa.workflow import candidates as finder
from cqa.workflow import lifecycle
from cqa.workflow import profile as profiler
from tests.regression.test_workflow_attribution import NOW, ROLES, fixture, task_label


def record(second, payload, kind="response_item"):
    return {"timestamp": (NOW + timedelta(seconds=second)).isoformat(), "type": kind, "payload": payload}


def usage(second, total=110):
    return record(second, {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": total - 10, "cached_input_tokens": 50, "output_tokens": 10},
        "total_token_usage": {"total_tokens": total}}}, "event_msg")


def write_logs(home, parents, roles=None, spawn=True, assignments=False):
    """Each session contributes 110 tokens, regardless of topology or names."""
    roles = roles or {}
    logdir = home / "sessions"
    logdir.mkdir()
    ids = {name: "session_" + name + "_123456789" for name in parents}
    rows = {}
    for i, (name, parent) in enumerate(parents.items()):
        metadata = {"session_id": ids[name], "source": "subagent" if parent else "cli", "model": "unpriced-model"}
        if parent:
            metadata["parent_session_id"] = ids[parent]
        if name in roles:
            metadata["role"] = roles[name]
        rows[name] = [record(i * 10, metadata, "session_meta"), usage(60 + i)]
    if spawn:
        for i, (name, parent) in enumerate(parents.items()):
            if not parent:
                continue
            label = task_label("private-task", roles[name], 1) if assignments else "opaque_worker_" + str(i)
            rows[parent].extend([
                record(i * 10, {"type": "function_call", "name": "collaboration.spawn_agent", "call_id": f"spawn-{i}",
                               "arguments": json.dumps({"task_name": label})}),
                record(i * 10 + .1, {"type": "function_call_output", "call_id": f"spawn-{i}",
                                    "output": json.dumps({"agent_id": ids[name]})}),
            ])
    for name, events in rows.items():
        (logdir / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in sorted(events, key=lambda r: r["timestamp"])))
    return ids




class TurnThroughputTests(unittest.TestCase):
    def test_weighted_turn_cadence_by_role_model_and_agent(self):
        family = finder.Family(members=["S-root", "S-child"], root="S-root", edges=[], family_key="W-test")
        root = finder.Session(path="root", session_key="S-root")
        root.roles.self_role["orchestrator"] = 3
        child = finder.Session(path="child", session_key="S-child")
        child.roles.self_role["implementer"] = 3
        sessions = {"S-root": root, "S-child": child}
        t0 = NOW
        parsed = {
            "S-root": lifecycle.ParsedSession(usage=[
                lifecycle.UsageRequest(t0, "S-root", 100, 50, 10, 0, "model-a", "high"),
                lifecycle.UsageRequest(t0 + timedelta(seconds=10), "S-root", 120, 60, 10, 0, "model-a", "high"),
                lifecycle.UsageRequest(t0 + timedelta(seconds=200), "S-root", 130, 70, 10, 0, "model-a", "high"),
            ]),
            "S-child": lifecycle.ParsedSession(usage=[
                lifecycle.UsageRequest(t0 + timedelta(seconds=1), "S-child", 200, 100, 20, 0, "model-b", "high"),
                lifecycle.UsageRequest(t0 + timedelta(seconds=21), "S-child", 210, 100, 20, 0, "model-b", "high"),
            ]),
        }
        out = profiler.build_turn_throughput(
            family, sessions, parsed, {"S-root": "ROOT", "S-child": "A01"},
            t0, t0 + timedelta(seconds=300), idle_gap_seconds=120,
        )
        self.assertEqual(out["overall"]["turns"], 5)
        # Qualifying gaps are 10s for root and 20s for child; the 190s idle gap is excluded.
        self.assertEqual(out["overall"]["observed_turn_transitions"], 2)
        self.assertAlmostEqual(out["overall"]["observed_turn_interval_seconds"], 30.0)
        self.assertAlmostEqual(out["overall"]["turns_per_observed_second"], 2 / 30)
        self.assertEqual(out["overall"]["observed_raw_tokens"], 360)
        self.assertAlmostEqual(out["overall"]["raw_tokens_per_observed_second"], 12.0)
        self.assertAlmostEqual(out["overall"]["turns_per_workflow_second"], 5 / 300)
        self.assertEqual(out["overall"]["output_tokens"], 70)
        self.assertAlmostEqual(out["overall"]["output_tokens_per_workflow_second"], 70 / 300)
        self.assertEqual(out["by_role"]["implementer"]["output_tokens"], 40)
        self.assertAlmostEqual(out["by_role"]["implementer"]["output_tokens_per_workflow_second"], 40 / 300)
        self.assertEqual(out["by_role"]["orchestrator/root"]["turns"], 3)
        self.assertAlmostEqual(out["by_role"]["orchestrator/root"]["turns_per_observed_second"], 1 / 10)
        self.assertEqual(out["by_role"]["implementer"]["turns"], 2)
        self.assertAlmostEqual(out["by_model"]["model-b"]["turns_per_observed_second"], 1 / 20)
        self.assertEqual(out["by_agent"]["A01"]["contributors"], 1)
        self.assertGreater(out["by_agent"]["A01"]["tokens_per_turn"], 0)

    def test_model_cadence_does_not_bridge_model_switches(self):
        family = finder.Family(members=["S-root"], root="S-root", edges=[], family_key="W-test")
        root = finder.Session(path="root", session_key="S-root")
        sessions = {"S-root": root}
        parsed = {"S-root": lifecycle.ParsedSession(usage=[
            lifecycle.UsageRequest(NOW, "S-root", 10, 0, 1, 0, "model-a", "high"),
            lifecycle.UsageRequest(NOW + timedelta(seconds=10), "S-root", 10, 0, 1, 0, "model-b", "high"),
            lifecycle.UsageRequest(NOW + timedelta(seconds=20), "S-root", 10, 0, 1, 0, "model-a", "high"),
        ])}
        out = profiler.build_turn_throughput(family, sessions, parsed, {"S-root": "ROOT"}, NOW, NOW + timedelta(seconds=30), 120)
        self.assertEqual(out["by_model"]["model-a"]["turns"], 2)
        self.assertEqual(out["by_model"]["model-a"]["observed_turn_transitions"], 0)
        self.assertIsNone(out["by_model"]["model-a"]["turns_per_observed_second"])

    def test_model_effort_throughput_is_populated_and_does_not_bridge_effort_switches(self):
        family = finder.Family(members=["S-root"], root="S-root", edges=[], family_key="W-test")
        sessions = {"S-root": finder.Session(path="root", session_key="S-root")}
        parsed = {"S-root": lifecycle.ParsedSession(usage=[
            lifecycle.UsageRequest(NOW, "S-root", 10, 0, 1, 0, "model-a", "high"),
            lifecycle.UsageRequest(NOW + timedelta(seconds=10), "S-root", 10, 0, 1, 0, "model-a", "high"),
            lifecycle.UsageRequest(NOW + timedelta(seconds=20), "S-root", 10, 0, 1, 0, "model-a", "medium"),
        ])}
        out = profiler.build_turn_throughput(family, sessions, parsed, {"S-root": "ROOT"}, NOW, NOW + timedelta(seconds=30), 120)
        high = next(x for x in out["by_model_effort"] if x["model"] == "model-a" and x["effort"] == "high")
        medium = next(x for x in out["by_model_effort"] if x["model"] == "model-a" and x["effort"] == "medium")
        self.assertEqual(high["stats"]["turns"], 2)
        self.assertEqual(high["stats"]["observed_turn_transitions"], 1)
        self.assertEqual(medium["stats"]["turns"], 1)
        self.assertEqual(medium["stats"]["observed_turn_transitions"], 0)

    def test_weighted_visible_output_speed_and_latency_percentiles(self):
        family = finder.Family(members=["S-root", "S-child"], root="S-root", edges=[], family_key="W-test")
        root = finder.Session(path="root", session_key="S-root")
        root.roles.self_role["orchestrator"] = 3
        child = finder.Session(path="child", session_key="S-child")
        child.roles.self_role["implementer"] = 3
        sessions = {"S-root": root, "S-child": child}
        parsed = {
            "S-root": lifecycle.ParsedSession(turn_timings=[
                lifecycle.TurnTiming(
                    NOW, "S-root", "T-1", "model-a", "high",
                    1000, 500, 150, 50, 100, 5.0, 2.0, 2.0, 1.0, True,
                    "timed_visible_response", True, 1, True,
                ),
            ]),
            "S-child": lifecycle.ParsedSession(turn_timings=[
                lifecycle.TurnTiming(
                    NOW + timedelta(seconds=10), "S-child", "T-2", "model-a", "high",
                    1000, 500, 240, 40, 200, 9.0, 4.0, 8.0, 2.0, True,
                    "timed_visible_response", True, 1, True,
                ),
            ]),
        }
        perf = profiler.build_turn_performance(family, sessions, parsed, {"S-root": "ROOT", "S-child": "A01"})
        # Weighted aggregation is (100+200)/(2+8)=30 tok/s, not mean(50,25)=37.5.
        self.assertAlmostEqual(perf["overall"]["visible_output_tokens_per_second"], 30.0)
        self.assertAlmostEqual(perf["overall"]["ttft_p50_seconds"], 3.0)
        self.assertAlmostEqual(perf["overall"]["ttft_p90_seconds"], 3.8)
        self.assertAlmostEqual(perf["overall"]["turn_duration_p50_seconds"], 7.0)
        self.assertEqual(perf["overall"]["task_turns"], 2)
        self.assertEqual(perf["overall"]["generation_quality"], "insufficient")
        self.assertIsNone(perf["overall"]["reasoning_tokens_per_second"])
        self.assertEqual(perf["by_model_effort"][0]["model"], "model-a")
        self.assertEqual(perf["by_model_effort"][0]["effort"], "high")
        self.assertEqual(perf["by_model_effort"][0]["stats"]["generation_timed_turns"], 2)

    def test_raw_turn_timing_parser_is_response_scoped_and_aggregates_visible_messages(self):
        def row(second, typ, payload):
            return {"timestamp": (NOW + timedelta(seconds=second)).isoformat(), "type": typ, "payload": payload}

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            turn1, turn2 = "turn-111111111", "turn-222222222"
            rows = [
                # First response: visible text with response-level usage. The turn-level
                # usage is intentionally huge to prove the parser does not use it.
                row(0, "event_msg", {"type": "item_completed", "turn_id": turn1,
                    "item": {"type": "Reasoning"}, "started_at_ms": 1_000, "completed_at_ms": 2_000}),
                row(1, "event_msg", {"type": "item_completed", "turn_id": turn1,
                    "item": {"type": "AgentMessage"}, "started_at_ms": 2_000, "completed_at_ms": 3_000}),
                row(2, "token_usage_record", {"turn_id": turn1, "response_id": "resp-1",
                    "usage": {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 150, "reasoning_output_tokens": 50},
                    "turn_token_usage": {"input_tokens": 50000, "cached_input_tokens": 40000, "output_tokens": 9000, "reasoning_output_tokens": 500}}),
                # Second response in the same task: two visible messages are valid and
                # their durations are summed.
                row(3, "event_msg", {"type": "item_completed", "turn_id": turn1,
                    "item": {"type": "AgentMessage"}, "started_at_ms": 4_000, "completed_at_ms": 5_000}),
                row(4, "event_msg", {"type": "item_completed", "turn_id": turn1,
                    "item": {"type": "AgentMessage"}, "started_at_ms": 5_000, "completed_at_ms": 7_000}),
                row(5, "token_usage_record", {"turn_id": turn1, "response_id": "resp-2",
                    "usage": {"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 80, "reasoning_output_tokens": 20},
                    "turn_token_usage": {"input_tokens": 50900, "cached_input_tokens": 40400, "output_tokens": 9080, "reasoning_output_tokens": 520}}),
                row(6, "event_msg", {"type": "task_complete", "turn_id": turn1,
                    "duration_ms": 7000, "time_to_first_token_ms": 1900}),
                # A response containing visible text plus a model function call must
                # be excluded because response-level output tokens include both.
                row(10, "event_msg", {"type": "item_completed", "turn_id": turn2,
                    "item": {"type": "AgentMessage"}, "started_at_ms": 10_000, "completed_at_ms": 11_000}),
                row(10.2, "response_item", {"type": "function_call", "turn_id": turn2, "name": "tool", "arguments": "{}"}),
                row(11, "token_usage_record", {"turn_id": turn2, "response_id": "resp-3",
                    "usage": {"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 80, "reasoning_output_tokens": 20}}),
            ]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
            parsed = lifecycle.parse_family_session(str(path), "S-test", ROLES)
            self.assertEqual(len(parsed.turn_timings), 3)
            first, second, third = parsed.turn_timings
            self.assertTrue(first.visible_generation_qualified)
            self.assertEqual(first.visible_output_tokens, 100)
            self.assertAlmostEqual(first.visible_generation_seconds, 1.0)
            self.assertAlmostEqual(first.time_to_first_token_seconds, 1.9)
            self.assertAlmostEqual(first.task_duration_seconds, 7.0)
            self.assertTrue(first.is_first_response_in_task)
            self.assertTrue(first.response_level_usage)
            self.assertEqual(first.visible_message_count, 1)
            self.assertTrue(second.visible_generation_qualified)
            self.assertEqual(second.visible_output_tokens, 60)
            self.assertAlmostEqual(second.visible_generation_seconds, 3.0)
            self.assertFalse(second.is_first_response_in_task)
            self.assertEqual(second.visible_message_count, 2)
            self.assertFalse(third.visible_generation_qualified)
            self.assertEqual(third.visible_output_tokens, 0)
            self.assertEqual(third.qualification_reason, "mixed_visible_and_tool_output")

    def test_generation_quality_thresholds_classify_thin_evidence(self):
        def acc(turns, timed):
            return {
                "turns": turns, "task_turns": turns, "contributors": {"A01"},
                "visible_responses": turns, "visible_timed_responses": turns,
                "generation_timed_turns": timed, "visible_output_tokens": timed * 100,
                "generation_seconds": float(timed or 1),
                "reasoning_tokens_all": 0, "reasoning_timed_turns": 0,
                "reasoning_tokens_timed": 0, "reasoning_seconds": 0.0,
                "ttft": [], "duration": [], "qualification_reasons": Counter(),
            }
        self.assertEqual(profiler._performance_finalize(acc(100, 4))["generation_quality"], "insufficient")
        self.assertEqual(profiler._performance_finalize(acc(64, 7))["generation_quality"], "low")
        self.assertEqual(profiler._performance_finalize(acc(40, 20))["generation_quality"], "good")


    def test_tool_call_output_does_not_contaminate_following_visible_response(self):
        def row(second, typ, payload):
            return {"timestamp": (NOW + timedelta(seconds=second)).isoformat(), "type": typ, "payload": payload}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            turn = "turn-output-boundary"
            rows = [
                row(0, "response_item", {"type": "function_call", "turn_id": turn, "call_id": "call-1"}),
                row(1, "token_usage_record", {"turn_id": turn, "response_id": "resp-tool", "usage": {
                    "input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 40, "reasoning_output_tokens": 10}}),
                # Tool result belongs to the completed tool-call response and must
                # not mark the next model response as mixed output.
                row(2, "response_item", {"type": "function_call_output", "call_id": "call-1"}),
                row(3, "event_msg", {"type": "item_completed", "turn_id": turn,
                    "item": {"type": "AgentMessage"}, "started_at_ms": 3_000, "completed_at_ms": 4_000}),
                row(4, "token_usage_record", {"turn_id": turn, "response_id": "resp-text", "usage": {
                    "input_tokens": 120, "cached_input_tokens": 0, "output_tokens": 62, "reasoning_output_tokens": 10}}),
            ]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
            parsed = lifecycle.parse_family_session(str(path), "S-test", ROLES)
            self.assertEqual(len(parsed.turn_timings), 2)
            self.assertFalse(parsed.turn_timings[0].visible_generation_qualified)
            self.assertTrue(parsed.turn_timings[1].visible_generation_qualified)
            self.assertEqual(parsed.turn_timings[1].qualification_reason, "timed_visible_response")
            self.assertEqual(parsed.turn_timings[1].visible_output_tokens, 52)

    def test_sanitized_multi_agent_performance_validation_corpus(self):
        fixture = ROOT / "tests" / "fixtures" / "performance_validation"
        manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
        timings = []
        for name in manifest["files"]:
            parsed = lifecycle.parse_family_session(str(fixture / name), name, ROLES)
            timings.extend(parsed.turn_timings)
        expected = manifest["expected"]
        visible = [t for t in timings if t.visible_message_count > 0]
        visible_timed = [t for t in visible if t.visible_message_seconds is not None and t.visible_message_seconds > 0]
        qualified = [t for t in timings if t.visible_generation_qualified]
        self.assertEqual(len(timings), expected["responses"])
        self.assertEqual(len(visible), expected["visible_responses"])
        self.assertEqual(len(visible_timed), expected["visible_timed_responses"])
        self.assertEqual(len(qualified), expected["qualified_visible_responses"])
        rate = sum(t.visible_output_tokens for t in qualified) / sum(t.visible_generation_seconds for t in qualified)
        self.assertAlmostEqual(rate, expected["weighted_visible_output_tokens_per_second"], places=1)
        self.assertEqual(sum(t.time_to_first_token_seconds is not None for t in timings), 9)

        keys = [Path(name).stem for name in manifest["files"]]
        family = finder.Family(members=keys, root=keys[0], edges=[], family_key="W-perf-validation")
        sessions = {}
        parsed = {}
        labels = {}
        roles = ["orchestrator", "orchestrator", "implementer", "implementer", "validator"]
        for idx, (name, role) in enumerate(zip(manifest["files"], roles)):
            key = Path(name).stem
            sess = finder.Session(path=str(fixture / name), session_key=key)
            sess.roles.self_role[role] = 3
            sessions[key] = sess
            parsed[key] = lifecycle.parse_family_session(str(fixture / name), key, ROLES)
            labels[key] = "ROOT" if idx == 0 else f"A{idx:02d}"
        perf = profiler.build_turn_performance(family, sessions, parsed, labels)
        overall = perf["overall"]
        self.assertEqual(overall["turns"], 93)
        self.assertEqual(overall["visible_responses"], 25)
        self.assertEqual(overall["visible_timed_responses"], 24)
        self.assertEqual(overall["qualified_visible_responses"], 9)
        self.assertAlmostEqual(overall["visible_generation_coverage"], 9 / 25)
        self.assertAlmostEqual(overall["visible_timing_coverage"], 24 / 25)
        self.assertEqual(overall["generation_quality"], "low")
        self.assertAlmostEqual(overall["visible_output_tokens_per_second"], 54.74601255, places=5)

    def test_raw_turn_timing_rejects_turn_level_usage_for_generation_rate(self):
        def row(second, typ, payload):
            return {"timestamp": (NOW + timedelta(seconds=second)).isoformat(), "type": typ, "payload": payload}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            turn = "turn-no-response-usage"
            rows = [
                row(0, "event_msg", {"type": "item_completed", "turn_id": turn,
                    "item": {"type": "AgentMessage"}, "started_at_ms": 1_000, "completed_at_ms": 2_000}),
                row(1, "token_usage_record", {"turn_id": turn, "turn_token_usage": {
                    "input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 150, "reasoning_output_tokens": 50}}),
            ]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
            parsed = lifecycle.parse_family_session(str(path), "S-test", ROLES)
            self.assertEqual(len(parsed.turn_timings), 1)
            timing = parsed.turn_timings[0]
            self.assertFalse(timing.visible_generation_qualified)
            self.assertEqual(timing.qualification_reason, "missing_response_level_usage")
            self.assertEqual(timing.visible_output_tokens, 0)

class GenericCliTests(unittest.TestCase):
    def run_profile(self, home, session, *options):
        output = home / "profile.json"
        result = subprocess.run([sys.executable, "-m", "cqa.workflow.profile", "--home", str(home),
                                 "--session", session, "--no-action-schema-audit", "--export-json", str(output),
                                 *options], capture_output=True, text=True, timeout=30, env=SUBPROCESS_ENV)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        data = json.loads(output.read_text())
        self.assertEqual(data["schema"], "codex-workflow-cost-profile-v6.10")
        self.assertIn("turn_throughput", data)
        self.assertIn("turn_performance", data)
        self.assertIn("response_efficiency", data)
        self.assertEqual(data["turn_throughput"]["overall"]["turns"], data["nested_attribution"]["total"]["requests"])
        self.assertNotIn(session, output.read_text() + result.stdout)
        nested = data["nested_attribution"]
        for metric in ("requests", "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
            self.assertEqual(sum(s["direct"][metric] for s in nested["sessions"]), nested["total"][metric])
            self.assertEqual(sum(u["attributed"][metric] for u in nested["units"]) + nested["unattributed"][metric], nested["total"][metric])
        return data, result.stdout

    def test_window_includes_continuing_workers_roles_and_clipped_lifetimes(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ids = write_logs(home, {"root": None, "lead": "root", "worker": "lead", "reviewer": "lead"},
                             {"root": "coordinator", "lead": "orchestrator", "worker": "implementer", "reviewer": "validator"},
                             assignments=True)
            after, before = NOW + timedelta(seconds=40), NOW + timedelta(seconds=65)
            report, _ = self.run_profile(home, ids["root"], "--after", after.isoformat(), "--before", before.isoformat())
            self.assertEqual(report["nested_attribution"]["total"]["total_tokens"], 440)
            self.assertEqual({r["role"] for r in report["nested_attribution"]["sessions"]},
                             {"coordinator", "orchestrator", "implementer", "validator"})
            window = report["analysis_window"]
            self.assertEqual(window["carry_in_policy"], "include")
            self.assertEqual(window["included_carry_in_sessions"], 3)
            self.assertEqual(window["included_carry_in_activity_in_window"]["total_tokens"], 330)
            self.assertEqual(window["excluded_carry_in_sessions"], 0)
            self.assertEqual(len(report["active_windows"]), 3)
            for active in report["active_windows"]:
                self.assertEqual(finder.parse_ts(active["start"]), after)
                self.assertLessEqual(finder.parse_ts(active["end"]), before)
            self.assertEqual(report["concurrency"]["peak_concurrent_children"], 3)

            excluded, text = self.run_profile(home, ids["root"], "--after", after.isoformat(),
                                               "--before", before.isoformat(), "--exclude-carry-in")
            self.assertEqual(excluded["nested_attribution"]["total"]["total_tokens"], 110)
            window = excluded["analysis_window"]
            self.assertEqual(window["carry_in_policy"], "exclude")
            self.assertEqual(window["included_carry_in_sessions"], 0)
            self.assertEqual(window["excluded_carry_in_sessions"], 3)
            self.assertEqual(window["excluded_carry_in_activity_in_window"]["total_tokens"], 330)
            self.assertIn("excluded from primary totals", text)

    def test_window_preserves_quiet_parent_without_counting_earlier_work(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ids = write_logs(home, {"root": None, "lead": "root", "worker": "lead"},
                             {"root": "coordinator", "lead": "orchestrator", "worker": "implementer"}, assignments=True)
            path = home / "sessions" / "worker.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-1]["timestamp"] = (NOW + timedelta(seconds=75)).isoformat()
            path.write_text("\n".join(json.dumps(r) for r in rows))
            report, _ = self.run_profile(home, ids["root"], "--after", (NOW + timedelta(seconds=70)).isoformat(),
                                        "--before", (NOW + timedelta(seconds=80)).isoformat())
            self.assertEqual(report["nested_attribution"]["total"]["total_tokens"], 110)
            sessions = {r["role"]: r for r in report["nested_attribution"]["sessions"]}
            self.assertEqual(set(sessions), {"coordinator", "orchestrator", "implementer"})
            self.assertEqual(sessions["orchestrator"]["direct"]["requests"], 0)
            self.assertIsNotNone(sessions["implementer"]["parent"])
            self.assertEqual(len(report["active_windows"]), 1)
            self.assertEqual(report["active_windows"][0]["role"], "implementer")
            self.assertEqual(report["workflow_analysis"]["sessions_without_lifetime_windows"], [])

    def test_excluding_carry_in_requires_a_start_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ids = write_logs(home, {"root": None})
            result = subprocess.run([sys.executable, "-m", "cqa.workflow.profile", "--home", d,
                                     "--session", ids["root"], "--exclude-carry-in"],
                                    capture_output=True, text=True, timeout=30, env=SUBPROCESS_ENV)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--exclude-carry-in requires --after", result.stderr)

    def test_carry_in_audit_stays_inside_explicit_analysis_subtree(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ids = write_logs(home, {"root": None, "lead": "root", "worker": "lead", "sibling": "root"},
                             {"root": "coordinator", "lead": "orchestrator", "worker": "implementer", "sibling": "validator"})
            for options, tokens, included, excluded in (([], 220, 1, 0), (["--exclude-carry-in"], 110, 0, 1)):
                report, _ = self.run_profile(home, ids["root"], "--analysis-root", ids["lead"],
                                            "--after", (NOW + timedelta(seconds=40)).isoformat(), *options)
                self.assertEqual(report["nested_attribution"]["total"]["total_tokens"], tokens)
                window = report["analysis_window"]
                self.assertEqual(window["included_carry_in_sessions"], included)
                self.assertEqual(window["excluded_carry_in_sessions"], excluded)
                self.assertEqual(window["excluded_carry_in_activity_in_window"]["total_tokens"], 110 * excluded)

    def test_standalone_session_and_date_window(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); ids = write_logs(home, {"root": None})
            full, _ = self.run_profile(home, ids["root"])
            self.assertEqual(full["nested_attribution"]["total"]["total_tokens"], 110)
            self.assertEqual(full["root_role"], "unknown")
            self.assertEqual(full["active_windows"], [])
            self.assertEqual(full["workflow_analysis"]["cycles_status"], "not_configured")
            self.assertIsNone(full["cycle_supervision_summary"])
            bounded, _ = self.run_profile(home, ids["root"], "--after", NOW.isoformat(),
                                          "--before", (NOW + timedelta(seconds=61)).isoformat())
            self.assertEqual(full["nested_attribution"]["total"], bounded["nested_attribution"]["total"])

    def test_root_role_override_marks_only_the_selected_root(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); ids = write_logs(home, {"root": None, "alpha": "root"})
            data, _ = self.run_profile(home, ids["root"], "--root-role", "coordinator")
            root = next(r for r in data["nested_attribution"]["sessions"] if r["parent"] is None)
            child = next(r for r in data["nested_attribution"]["sessions"] if r["parent"] is not None)
            self.assertEqual(root["role"], "coordinator")
            self.assertEqual(root["role_confidence"], "declared-root-role")
            self.assertNotEqual(child["role_confidence"], "declared-root-role")

    def test_unknown_flat_and_nested_workers_are_in_core_activity(self):
        for parents in ({"root": None, "alpha": "root", "beta": "root"},
                        {"root": None, "alpha": "root", "beta": "alpha"}):
            with self.subTest(parents=parents), tempfile.TemporaryDirectory() as d:
                home = Path(d); ids = write_logs(home, parents)
                data, _ = self.run_profile(home, ids["root"])
                self.assertEqual(data["nested_attribution"]["total"]["total_tokens"], 330)
                self.assertEqual(len(data["active_windows"]), 2)
                self.assertEqual({w["role"] for w in data["active_windows"]}, {"unknown"})
                self.assertEqual(data["concurrency"]["peak_concurrent_children"], 2)
                self.assertEqual(data["nested_attribution"]["coverage"]["explicit_assignments"], 0)
                self.assertEqual(data["lingering_candidates"], [])  # unknown is not a shared job title

    def test_roles_and_profiles_do_not_filter_accounting_or_core_concurrency(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ids = write_logs(home, {"root": None, "alpha": "root", "beta": "root"},
                             {"alpha": "writer", "beta": "reviewer"})
            generic, _ = self.run_profile(home, ids["root"])
            custom, _ = self.run_profile(home, ids["root"], "--roles", "writer", "reviewer", "--stage-roles", "writer",
                                        "--cycle-roles", "writer", "reviewer", "--successor", "writer=reviewer")
            staged, _ = self.run_profile(home, ids["root"], "--workflow-profile", "staged")
            for report in (custom, staged):
                self.assertEqual(generic["nested_attribution"]["total"], report["nested_attribution"]["total"])
                self.assertEqual(generic["concurrency"], report["concurrency"])
                self.assertEqual(generic["analysis_window"], report["analysis_window"])
            self.assertEqual({w["role"] for w in custom["active_windows"]}, {"writer", "reviewer"})
            filtered = custom["workflow_analysis"]["role_filtered_activity"]
            self.assertEqual(len(filtered["agents"]), 1)
            self.assertEqual(filtered["concurrency"]["peak_concurrent_children"], 1)
            self.assertEqual(custom["cycles"][0]["first_role"], "writer")
            self.assertEqual(custom["cycles"][0]["second_role"], "reviewer")
            self.assertEqual(generic["successor_map_for_lingering_candidates"], {})
            self.assertIn("implementer", staged["successor_map_for_lingering_candidates"])
            self.assertEqual(staged["workflow_analysis"]["cycles_status"], "unavailable")

    def test_missing_spawn_does_not_erase_usage_or_invent_windows(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); ids = write_logs(home, {"root": None, "alpha": "root"}, spawn=False)
            data, text = self.run_profile(home, ids["root"])
            self.assertEqual(data["nested_attribution"]["total"]["total_tokens"], 220)
            self.assertEqual(data["active_windows"], [])
            self.assertEqual(len(data["workflow_analysis"]["sessions_without_lifetime_windows"]), 1)
            self.assertIn("does not prove no worker activity", text)

    def test_custom_assignment_roles_reach_lifecycle_export(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); ids = write_logs(home, {"root": None, "alpha": "root"}, {"alpha": "researcher"}, assignments=True)
            path = home / "lifecycle.json"
            result = subprocess.run([sys.executable, "-m", "cqa.workflow.lifecycle", "--home", str(home),
                                     "--session", ids["root"], "--roles", "researcher", "--export-json", str(path)],
                                    capture_output=True, text=True, timeout=30, env=SUBPROCESS_ENV)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            data = json.loads(path.read_text())
            child = next(row for row in data["sessions"] if not row["root"])
            self.assertEqual(child["responsibility"], "researcher")
            self.assertEqual(child["role_confidence"], "trusted-spawn-label")
            self.assertIn("researcher", result.stdout)
            self.assertNotIn("private-task", path.read_text())
            self.assertNotIn(ids["root"], path.read_text())

    def test_explicit_leaf_root_does_not_adopt_sibling_activity(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d); ids = write_logs(home, {"root": None, "alpha": "root", "beta": "root"})
            data, _ = self.run_profile(home, ids["root"], "--analysis-root", ids["alpha"])
            self.assertEqual(data["nested_attribution"]["total"]["total_tokens"], 110)
            self.assertEqual(data["analysis_window"]["primary_sessions"], 1)


class GenericStructureTests(unittest.TestCase):
    def test_configured_cycles_require_complete_isolated_lifetimes(self):
        f, sessions, parsed, labels = fixture()
        windows = []
        for index, (key, role, start, end) in enumerate((
                ("impl", "writer", 10, 20), ("val", "reviewer", 21, 30)), 1):
            sessions[key].last_ts = NOW + timedelta(seconds=end)
            windows.append(profiler.ActiveWindow(index, role, key, labels[key],
                           NOW + timedelta(seconds=start), NOW + timedelta(seconds=start),
                           NOW + timedelta(seconds=end), "spawn-result-id", "high"))
        def cycles(ws):
            return profiler.build_cycles(f, sessions, parsed, ws, {}, 300, ("writer", "reviewer"))
        clean = cycles(windows)
        self.assertEqual(len(clean), 1)
        self.assertTrue(clean[0].ratio_eligible)
        self.assertEqual(profiler.build_cycles(f, sessions, parsed, windows, {}, 300), [])

        # Even an unclassified worker prevents an isolated supervision claim.
        unknown = profiler.ActiveWindow(3, "unknown", "lead", labels["lead"], NOW, NOW,
                                        NOW + timedelta(seconds=40), "spawn-result-id", "high")
        overlapping = cycles([unknown, *windows])
        self.assertFalse(overlapping[0].ratio_eligible)
        self.assertEqual(overlapping[0].other_active_roles, ("unknown",))

        # A selected time window must not turn partial evidence into a full cycle.
        windows[1].end = NOW + timedelta(seconds=25)
        truncated = cycles(windows)
        self.assertEqual(truncated[0].quality, "window-truncated")
        self.assertFalse(truncated[0].ratio_eligible)

    def test_discovery_and_ranking_are_independent_of_role_names(self):
        f, sessions, _, _ = fixture()
        for s in sessions.values():
            s.input_tokens = 100
            s.output_tokens = 10
        edges = [finder.Edge("root", "lead", "explicit-parent-id", "high", 1, 0),
                 finder.Edge("lead", "impl", "explicit-parent-id", "high", 1, 0)]
        before = finder.build_families(sessions, edges)
        for s in sessions.values():
            s.roles.self_role = Counter({"unfamiliar": 2})
            s.roles.routing_role = Counter({"orchestrator": 100})
        after = finder.build_families(sessions, edges)
        self.assertEqual([(x.family_key, x.root, x.score, x.sample_quality) for x in before],
                         [(x.family_key, x.root, x.score, x.sample_quality) for x in after])
        self.assertEqual(len(after), 2)  # linked tree plus standalone val

    def test_ambiguous_active_roots_require_explicit_selection(self):
        f, sessions, parsed, _ = fixture()
        for p in parsed.values():
            p.actions.clear()
        result = profiler.select_analysis_root(f, sessions, parsed, NOW, None, None)
        self.assertIsNone(result[0])
        explicit = profiler.select_analysis_root(f, sessions, parsed, NOW, None, "root")
        self.assertEqual(explicit[0], "root")


if __name__ == "__main__":
    unittest.main()
