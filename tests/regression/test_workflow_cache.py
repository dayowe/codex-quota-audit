"""Cache correctness, extraction parity and workflow selection boundaries."""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.regression.test_generic_workflow import write_logs, record
from cqa import cli
from cqa.workflow import candidates as finder, lifecycle, profile, telemetry, response_efficiency
from cqa.workflow.cache import Index, encode
from cqa.quota import audit


class WorkflowCacheTests(unittest.TestCase):
    def fixture(self, directory):
        home = Path(directory)
        ids = write_logs(home, {"root": None, "alpha": "root", "beta": "alpha"},
                         roles={"root": "coordinator", "alpha": "implementer", "beta": "validator"},
                         assignments=True)
        return home, ids

    def run_profile(self, home, selector, *extra):
        output = home / "profile.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = profile.main(["--home", str(home), "--family", selector,
                               "--pause-store", str(home / "pauses"),
                               "--export-json", str(output), *extra])
        self.assertEqual(rc, 0)
        result = json.loads(output.read_text())
        # Only the generation timestamp is allowed to change across runs.
        result.pop("generated_at", None)
        return result

    def test_cold_warm_and_uncached_reports_match_and_warm_reads_no_logs(self):
        with tempfile.TemporaryDirectory() as d:
            home, ids = self.fixture(d)
            cold = self.run_profile(home, ids["root"])
            with mock.patch.object(telemetry, "extract", side_effect=AssertionError("read telemetry")), \
                 mock.patch.object(finder, "summary_consumer", side_effect=AssertionError("read discovery")):
                warm = self.run_profile(home, ids["root"])
            direct = self.run_profile(home, ids["root"], "--no-cache")
            self.assertEqual(cold, warm)
            self.assertEqual(cold, direct)

    def test_changed_deleted_and_replaced_files_are_refreshed(self):
        with tempfile.TemporaryDirectory() as d:
            home, ids = self.fixture(d)
            first = finder.discover_workflow_families(d)
            self.assertEqual(first.stats["processed"], 3)
            with Index(d) as index:
                self.assertEqual(index.identifier_paths(finder.fp(ids["alpha"]), list(finder.DEFAULT_ROLES)), {str(home / "sessions" / "alpha.jsonl")})
            second = finder.discover_workflow_families(d)
            self.assertEqual(second.stats["processed"], 0)
            path = home / "sessions" / "alpha.jsonl"
            with path.open("a") as stream:
                stream.write("\n" + json.dumps(record(200, {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 999, "output_tokens": 10},
                    "total_token_usage": {"input_tokens": 999, "output_tokens": 10, "total_tokens": 1009}}}, "event_msg")))
            changed = finder.discover_workflow_families(d)
            self.assertEqual(changed.stats["processed"], 1)
            self.assertTrue(any(s.input_tokens == 1099 for s in changed.sessions.values()))
            stat = path.stat()
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            replacement.replace(path)
            self.assertEqual(finder.discover_workflow_families(d).stats["processed"], 1)
            path.unlink()
            self.assertEqual(len(finder.discover_workflow_families(d).sessions), 2)
            with Index(d) as index:
                self.assertEqual(index.connection.execute("SELECT COUNT(*) FROM entries WHERE path=?", (str(path),)).fetchone()[0], 0)

    def test_parser_version_and_role_vocabulary_invalidate_entries(self):
        with tempfile.TemporaryDirectory() as d:
            self.fixture(d)
            finder.discover_workflow_families(d)
            self.assertEqual(finder.discover_workflow_families(d, ["custom-role"]).stats["processed"], 3)
            with mock.patch("cqa.workflow.cache.EXTRACTION_VERSION", 99):
                self.assertEqual(finder.discover_workflow_families(d).stats["processed"], 3)

    def test_no_cache_writes_nothing_and_rebuild_refreshes_all_files(self):
        with tempfile.TemporaryDirectory() as d:
            home, _ = self.fixture(d)
            finder.discover_workflow_families(d, use_cache=False)
            self.assertFalse((home / "codex-quota-audit").exists())
            finder.discover_workflow_families(d)
            self.assertEqual(finder.discover_workflow_families(d, rebuild_cache=True).stats["processed"], 3)

    def test_parallel_indexing_preserves_graph_and_recent_selection_keeps_ancestors(self):
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as d:
            home, ids = self.fixture(d)
            path = home / "sessions" / "beta.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for row in rows:
                row["timestamp"] = (datetime.fromisoformat(row["timestamp"]) + timedelta(days=10)).isoformat()
            path.write_text("\n".join(json.dumps(row) for row in rows))
            serial = finder.discover_workflow_families(d, use_cache=False)
            expected = home / "serial.json"
            actual = home / "parallel.json"
            finder.export_json(str(expected), serial.families, serial.sessions, 10)
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
            result = subprocess.run([sys.executable, "-m", "cqa.workflow.candidates", "--home", d,
                                     "--workers", "2", "--recent-days", "3", "--export-json", str(actual)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(expected.read_text()), json.loads(actual.read_text()))
            recent = finder.discover_workflow_families(d, recent_days=3)
            family = lifecycle.resolve_family(ids["beta"], recent.families, recent.sessions)
            self.assertEqual(len(family.members), 3)

    def test_damaged_database_falls_back_and_damaged_entry_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as d:
            home, _ = self.fixture(d)
            finder.discover_workflow_families(d)
            path = home / "codex-quota-audit" / "cache" / "workflow.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE entries SET data='invalid' WHERE kind='discovery'")
            connection.close()
            self.assertEqual(finder.discover_workflow_families(d).stats["processed"], 3)
            path.write_bytes(b"broken database")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                result = finder.discover_workflow_families(d)
            self.assertEqual(len(result.sessions), 3)
            self.assertIn("cache unavailable", errors.getvalue())

    def test_interrupted_indexing_resumes_from_completed_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.fixture(d)
            original = finder.extract_summary
            calls = 0
            def interrupt(path, roles):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt
                return original(path, roles)
            with mock.patch.object(finder, "extract_summary", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    finder.discover_workflow_families(d)
            resumed = finder.discover_workflow_families(d)
            self.assertEqual(resumed.stats["cached"], 1)
            self.assertEqual(resumed.stats["processed"], 2)

    def test_reuse_of_latest_discovery_and_explicit_scopes(self):
        with tempfile.TemporaryDirectory() as d:
            home, ids = self.fixture(d)
            discovery = finder.discover_workflow_families(d)
            with mock.patch.object(finder, "discover_workflow_families", side_effect=AssertionError("duplicate discovery")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(profile.main(["--home", d, "--family", ids["root"]], discovery=discovery), 0)
            family = self.run_profile(home, ids["alpha"])
            subtree = self.run_profile(home, ids["alpha"], "--scope", "subtree")
            session = self.run_profile(home, ids["alpha"], "--scope", "session")
            self.assertEqual(len(family["nested_attribution"]["sessions"]), 3)
            self.assertEqual(len(subtree["nested_attribution"]["sessions"]), 2)
            self.assertEqual(len(session["nested_attribution"]["sessions"]), 1)

    def test_unified_latest_discovers_once_even_without_cache(self):
        with tempfile.TemporaryDirectory() as d:
            self.fixture(d)
            with mock.patch.object(finder, "discover_workflow_families", wraps=finder.discover_workflow_families) as discover, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["workflow", "profile", "latest", "--home", d, "--no-open", "--quiet", "--no-cache"]), 0)
            self.assertEqual(discover.call_count, 1)

    def test_single_pass_evidence_matches_reference_extractors_and_excludes_content(self):
        with tempfile.TemporaryDirectory() as d:
            home, _ = self.fixture(d)
            path = home / "sessions" / "root.jsonl"
            secret = "PRIVATE_PROMPT_RESPONSE_TOOL_OUTPUT"
            events = [record(1, {"type": "task_started", "turn_id": "private-turn-id"}, "event_msg"),
                      record(2, {"type": "item_completed", "turn_id": "private-turn-id", "started_at_ms": 1000,
                                 "completed_at_ms": 2000, "item": {"type": "AgentMessage", "text": secret}}, "event_msg"),
                      record(3, {"type": "function_call", "name": "exec_command", "call_id": "private-call-id", "arguments": json.dumps({"cmd": "cat /private/file"})}),
                      record(4, {"type": "function_call_output", "call_id": "private-call-id", "output": secret}),
                      record(5, {"type": "token_usage_record", "turn_id": "private-turn-id", "usage": {"output_tokens": 30, "reasoning_output_tokens": 10}}, "token_usage_record"),
                      record(6, {"type": "task_complete", "turn_id": "private-turn-id"}, "event_msg")]
            with path.open("a") as stream:
                stream.write("\n" + "\n".join(json.dumps(e) for e in events))
            observation = telemetry.extract(str(path), "S-test", finder.DEFAULT_ROLES)
            self.assertEqual(encode(observation.parsed), encode(lifecycle.parse_family_session(str(path), "S-test", finder.DEFAULT_ROLES)))
            for start, end in [(None, None), (record(2, {})["timestamp"], record(5, {})["timestamp"])]:
                start, end = finder.parse_ts(start), finder.parse_ts(end)
                expected = profile.scan_raw_compaction_session(str(path), "S-test", start, end)
                self.assertEqual(encode(telemetry.compaction_view(observation, start, end)), encode(expected))
                direct = response_efficiency.aggregate_tool_excluded(response_efficiency.parse_tool_excluded_tasks(str(path), after=start, before=end))
                cached = response_efficiency.aggregate_tool_excluded(response_efficiency.parse_tool_excluded_tasks(str(path), after=start, before=end,
                    records=(json.dumps(r).encode() for r in observation.response_records)))
                self.assertEqual(direct, cached)
            saved = json.dumps(encode(observation))
            for private in (secret, "private-turn-id", "private-call-id", "/private/file"):
                self.assertNotIn(private, saved)

    def test_discovery_seeds_shared_quota_observations_and_prices_are_recomputed(self):
        with tempfile.TemporaryDirectory() as d:
            home, _ = self.fixture(d)
            path = home / "sessions" / "root.jsonl"
            with path.open("a") as stream:
                stream.write("\n" + json.dumps(record(200, {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 100, "output_tokens": 10},
                    "total_token_usage": {"input_tokens": 100, "output_tokens": 10}},
                    "rate_limits": {"limit_id": "codex", "primary": {"used_percent": 1, "window_minutes": 10080, "resets_at": 1800000000}}}, "event_msg")))
            finder.discover_workflow_families(d)
            with mock.patch.object(audit, "parse_file", side_effect=AssertionError("quota reread")):
                first, _ = audit.load_events(d, {"unpriced-model": (1, 1, 1)}, 10080, 2, 20, .1, .1, 20)
                second, _ = audit.load_events(d, {"unpriced-model": (2, 2, 2)}, 10080, 2, 20, .1, .1, 20)
            self.assertEqual(len(first), 1)
            self.assertAlmostEqual(first[0].api_usd * 2, second[0].api_usd)
            # Stored observations never freeze a user-supplied price override.
            with Index(d) as index:
                for path in finder.file_paths(d):
                    cached, _ = index.get(path, "quota", 10080)
                    self.assertIsNotNone(cached)

    def test_timing_projection_preserves_short_ids_and_numeric_string_usage(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "timing.jsonl"
            rows = [record(0, {"type": "task_started", "turn_id": "t"}, "event_msg"),
                    record(.5, {"type": "item_completed", "turn_id": "t", "started_at_ms": 1000,
                                "completed_at_ms": 1500, "item": {"type": "Reasoning", "text": "private"}}, "event_msg"),
                    record(1, {"type": "function_call", "call_id": "c", "name": "exec_command"}),
                    record(2, {"type": "function_call_output", "call_id": "c", "output": "private"}),
                    record(3, {"type": "token_usage_record", "turn_id": "t", "usage": {"output_tokens": "20", "reasoning_output_tokens": "5"}}, "token_usage_record"),
                    record(4, {"type": "task_complete", "turn_id": "t"}, "event_msg")]
            path.write_text("\n".join(json.dumps(row) for row in rows))
            observation = telemetry.extract(str(path), "S-test", finder.DEFAULT_ROLES)
            expected = response_efficiency.aggregate_tool_excluded(response_efficiency.parse_tool_excluded_tasks(str(path)))
            actual = response_efficiency.aggregate_tool_excluded(response_efficiency.parse_tool_excluded_tasks(str(path),
                records=(json.dumps(r).encode() for r in observation.response_records)))
            self.assertEqual(actual, expected)
            self.assertEqual(actual["qualified_tasks"], 1)
            self.assertEqual(actual["reasoning_items"], 1)


if __name__ == "__main__":
    unittest.main()
