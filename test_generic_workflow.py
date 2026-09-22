"""Portable workflow fixtures: role-independent accounting and explicit limits."""
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path

import find_workflow_candidates as finder
import extract_workflow_lifecycle as lifecycle
import profile_workflow_cost as profiler
from test_workflow_attribution import NOW, ROLES, fixture, task_label


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


class GenericCliTests(unittest.TestCase):
    def run_profile(self, home, session, *options):
        output = home / "profile.json"
        result = subprocess.run([sys.executable, str(Path(profiler.__file__)), "--home", str(home),
                                 "--session", session, "--no-action-schema-audit", "--export-json", str(output),
                                 *options], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        data = json.loads(output.read_text())
        self.assertNotIn(session, output.read_text() + result.stdout)
        nested = data["nested_attribution"]
        for metric in ("requests", "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
            self.assertEqual(sum(s["direct"][metric] for s in nested["sessions"]), nested["total"][metric])
            self.assertEqual(sum(u["attributed"][metric] for u in nested["units"]) + nested["unattributed"][metric], nested["total"][metric])
        return data, result.stdout

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
            result = subprocess.run([sys.executable, str(Path(lifecycle.__file__)), "--home", str(home),
                                     "--session", ids["root"], "--roles", "researcher", "--export-json", str(path)],
                                    capture_output=True, text=True, timeout=30)
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
