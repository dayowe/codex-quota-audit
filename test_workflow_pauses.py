"""Pause accounting and offline review, using synthetic logs and temporary stores."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import extract_workflow_lifecycle as lifecycle
import profile_workflow_cost as profiler
import workflow_pauses as pauses
from test_generic_workflow import record, usage


START = datetime(2026, 9, 20, tzinfo=timezone.utc)


def iso(hours):
    return (START + timedelta(hours=hours)).isoformat()


def evidence():
    return {"schema": pauses.SCHEMA, "analysis_root": "S-pause-fixture", "analysis_start": iso(0),
            "analysis_end": iso(12), "timeline_columns": ["timestamp", "requests", "raw_tokens"],
            "usage_timeline": [[iso(h), 1, 100] for h in [1, 2, 10, 11]]}


def decision(left, right, status="confirmed", candidate=None):
    a, b = candidate or (iso(left), iso(right))
    return {"candidate_start": a, "candidate_end": b, "start": iso(left), "end": iso(right),
            "status": status, "boundary_source": "user-supplied"}


class PauseAccountingTests(unittest.TestCase):
    def test_gaps_are_hypotheses_with_no_bounding_usage_removed(self):
        result = pauses.analyze(evidence(), [], 120)
        self.assertEqual(len(result["quiet_intervals"]), 1)
        gap = result["quiet_intervals"][0]
        self.assertEqual(gap["status"], "unclassified")
        self.assertEqual(result["elapsed"], result["excluding_confirmed_pauses"])
        hypothetical = result["if_unclassified_gaps_were_pauses"]
        self.assertEqual(hypothetical["raw_tokens"], 400)
        self.assertEqual(hypothetical["excluded_requests"], 0)
        self.assertAlmostEqual(hypothetical["seconds"], 4*3600, places=4)

    def test_confirmation_rejection_and_removal_preserve_original_totals(self):
        raw = evidence()
        gap = pauses.analyze(raw, [], 120)["quiet_intervals"][0]
        row = {"candidate_start": gap["start"], "candidate_end": gap["end"],
               "start": gap["start"], "end": gap["end"], "status": "confirmed", "boundary_source": "inferred"}
        confirmed = pauses.analyze(raw, [row], 120)
        self.assertIsNone(confirmed["if_unclassified_gaps_were_pauses"])
        self.assertEqual(confirmed["elapsed"]["raw_tokens"], 400)
        self.assertEqual(confirmed["quiet_intervals"][0]["status"], "confirmed")
        rejected = pauses.analyze(raw, [{**row, "status": "rejected"}], 120)
        self.assertEqual(rejected["elapsed"], rejected["excluding_confirmed_pauses"])
        self.assertIsNone(rejected["if_unclassified_gaps_were_pauses"])
        self.assertEqual(pauses.analyze(raw, [], 120)["quiet_intervals"][0]["status"], "unclassified")

    def test_edited_overlapping_pauses_remove_usage_and_time_once(self):
        result = pauses.analyze(evidence(), [decision(1, 3), decision(2, 10)], 120)
        adjusted = result["excluding_confirmed_pauses"]
        self.assertEqual(adjusted["seconds"], 3*3600)
        self.assertEqual(adjusted["excluded_raw_tokens"], 200)
        self.assertEqual(adjusted["raw_tokens"], 200)  # end=10 remains included
        self.assertEqual(adjusted["excluded_requests"], 2)
        self.assertEqual(result["elapsed"]["raw_tokens"], 400)

    def test_pauses_clip_to_window_and_zero_duration_has_no_rate(self):
        result = pauses.analyze(evidence(), [decision(-1, 14)], 120)
        adjusted = result["excluding_confirmed_pauses"]
        self.assertEqual(adjusted["seconds"], 0)
        self.assertEqual(adjusted["raw_tokens"], 0)
        self.assertIsNone(adjusted["raw_million_tokens_per_hour"])
        self.assertEqual(adjusted["excluded_raw_tokens"], 400)

    def test_timezone_offsets_and_half_open_boundaries(self):
        row = decision(1, 2)
        row.update(start="2026-09-20T03:00:00+02:00", end="2026-09-20T04:00:00+02:00")
        result = pauses.analyze(evidence(), [row], 120)
        self.assertEqual(result["excluding_confirmed_pauses"]["excluded_raw_tokens"], 100)
        with self.assertRaises(ValueError):
            pauses.interval("2026-09-20T03:00:00", iso(5))
        with self.assertRaises(ValueError):
            pauses.interval(iso(5), iso(3))

    def test_silence_at_edges_and_missing_usage_are_not_automatic_pauses(self):
        raw = evidence(); raw["usage_timeline"] = [[iso(5), 1, 100]]
        self.assertEqual(pauses.analyze(raw, [])["quiet_intervals"], [])
        raw["usage_timeline"] = []
        result = pauses.analyze(raw, [])
        self.assertEqual(result["quiet_intervals"], [])
        self.assertEqual(result["elapsed"]["raw_tokens"], 0)

    def test_malformed_evidence_is_not_silently_accepted(self):
        for rows in [[[iso(2), 1, 100], [iso(1), 1, 100]], [[iso(1), -1, 10]], [[iso(12), 1, 10]]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                pauses.analyze({**evidence(), "usage_timeline": rows}, [])
        for threshold in [0, -1, float("nan"), float("inf")]:
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                pauses.analyze(evidence(), [], threshold)

    def test_all_workers_and_simultaneous_calls_participate(self):
        calls = [lifecycle.UsageRequest(START + timedelta(hours=h), name, 100, 50, 10, 4, "m", "high")
                 for h, name in [(1, "root"), (2, "child"), (2, "other"), (3, "child"), (4, "root")]]
        raw = pauses.make_evidence("S-fixture", START, START+timedelta(hours=5), calls)
        self.assertEqual(raw["usage_timeline"][1][1:], [2, 220])
        result = pauses.analyze(raw, [], 120)
        self.assertEqual(result["quiet_intervals"], [])  # root silence isn't family silence
        self.assertEqual(result["elapsed"]["raw_tokens"], 550)


class PauseReviewTests(unittest.TestCase):
    def report(self, path):
        result = pauses.analyze(evidence(), [], 120)
        obj = {"pause_analysis": result, "comparison_metrics": {"workflow_raw_tokens": 400}, "family": "W-original"}
        path.write_text(json.dumps(obj))
        return obj

    def review(self, path, directory, answers, output=None):
        with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=[*answers, ""]), \
                patch.object(profiler.finder, "file_paths", side_effect=AssertionError("must not scan")), \
                contextlib.redirect_stdout(io.StringIO()) as stream:
            pauses.review_report(path, directory, output)
        return stream.getvalue()

    def test_offline_review_persists_and_supports_edit_reject_remove(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)/"state"; path=Path(temp)/"report.json"
            self.report(path); original=path.read_bytes()
            text=self.review(path, directory, ["1"])
            entries, _=pauses.load_annotations(directory, "S-pause-fixture")
            self.assertEqual(entries[0]["status"], "confirmed")
            self.assertEqual(entries[0]["boundary_source"], "inferred")
            self.assertIn("approximate", text)
            self.assertEqual(path.read_bytes(), original)
            output=Path(temp)/"reviewed.json"
            text=self.review(path, directory, ["2", iso(1), iso(3)], output)
            entries, _=pauses.load_annotations(directory, "S-pause-fixture")
            self.assertEqual(entries[0]["boundary_source"], "user-supplied")
            self.assertIn("Usage DURING confirmed pauses: 200", text)
            saved=json.loads(output.read_text())
            self.assertEqual(saved["comparison_metrics"]["workflow_raw_tokens"], 400)
            self.assertEqual(saved["pause_analysis"]["excluding_confirmed_pauses"]["raw_tokens"], 200)
            self.review(path, directory, ["3"])
            self.assertEqual(pauses.load_annotations(directory, "S-pause-fixture")[0][0]["status"], "rejected")
            self.review(path, directory, ["4"])
            self.assertEqual(pauses.load_annotations(directory, "S-pause-fixture")[0], [])

    def test_cancellation_and_invalid_times_do_not_save(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"report.json"; self.report(path); directory=Path(temp)/"state"
            with self.assertRaises(EOFError):
                self.review(path, directory, [EOFError()])
            self.assertFalse(directory.exists())
            text=self.review(path, directory, ["2", "bad", iso(3), ""])
            self.assertIn("Unchanged", text)
            self.assertFalse(directory.exists())

    def test_manual_pause_without_a_detected_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"report.json"; obj=self.report(path)
            obj["pause_analysis"]["quiet_gap_minutes"]=600
            path.write_text(json.dumps(obj))
            text=self.review(path, Path(temp)/"state", ["a", iso(1), iso(3)])
            self.assertIn("Usage DURING confirmed pauses: 200", text)
            entries, _=pauses.load_annotations(Path(temp)/"state", "S-pause-fixture")
            self.assertEqual(entries[0]["boundary_source"], "user-supplied")

    def test_existing_report_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"report.json"; self.report(path)
            with self.assertRaises(ValueError):
                self.review(path, Path(temp)/"state", [], path)

    def test_corrupt_store_and_concurrent_changes_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            path=pauses.save_annotations(temp, "S-pause-fixture", [decision(2, 8)], None)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "changed during review"):
                pauses.save_annotations(temp, "S-pause-fixture", [], None)
            self.assertEqual(len(pauses.load_annotations(temp, "S-pause-fixture")[0]), 1)
            path.write_text("{broken")
            with self.assertRaisesRegex(ValueError, "Cannot use pause annotations"):
                pauses.load_annotations(temp, "S-pause-fixture")

    def test_annotations_follow_root_not_family_and_leave_other_roots_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            pauses.save_annotations(temp, "S-pause-fixture", [decision(2, 8)], None)
            entries, _=pauses.load_annotations(temp, "S-pause-fixture")
            newer={**evidence(), "analysis_end": iso(24)}
            result=pauses.analyze(newer, entries)
            self.assertEqual(result["excluding_confirmed_pauses"]["excluded_seconds"], 6*3600)
            self.assertEqual(pauses.load_annotations(temp, "S-other")[0], [])

    def test_review_cli_bypasses_log_discovery_and_old_reports_explain_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"report.json"; self.report(path)
            args=["profiler", "--review-pauses", str(path), "--pause-store", str(Path(temp)/"state")]
            with patch.object(sys, "argv", args), patch.object(sys.stdin, "isatty", return_value=True), \
                    patch("builtins.input", side_effect=["3", ""]), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(profiler.finder, "file_paths", side_effect=AssertionError("must not scan")):
                self.assertEqual(profiler.main(), 0)
            path.write_text('{"version":"6.0"}')
            with self.assertRaisesRegex(ValueError, "regenerate"):
                self.review(path, Path(temp)/"state", [])

    def test_noninteractive_review_rejects_without_hanging(self):
        result=subprocess.run([sys.executable, str(Path(profiler.__file__)), "--review-pauses", "unused.json"],
                              input="", capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires an interactive terminal", result.stderr)

    @unittest.skipUnless(os.name == "posix", "PTY integration test requires POSIX")
    def test_real_terminal_review_without_session_or_log_access(self):
        import pty
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"report.json"; self.report(path); state=Path(temp)/"state"
            master, slave=pty.openpty()
            try:
                proc=subprocess.Popen([sys.executable, str(Path(profiler.__file__)), "--review-pauses", str(path),
                                       "--pause-store", str(state), "--home", str(Path(temp)/"does-not-exist")],
                                      stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                os.close(slave); slave=None
                os.write(master, b"1\n\n")
                try:
                    stdout, stderr=proc.communicate(timeout=10)
                except BaseException:
                    proc.kill(); proc.communicate()
                    raise
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertIn("Saved decisions", stdout)
                self.assertNotIn("Rebuilding", stdout)
                self.assertEqual(pauses.load_annotations(state, "S-pause-fixture")[0][0]["status"], "confirmed")
            finally:
                os.close(master)
                if slave is not None:
                    os.close(slave)


class PauseCliTests(unittest.TestCase):
    def test_profile_export_review_and_later_profile_with_worker_spanning_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)/"logs"; (home/"sessions").mkdir(parents=True)
            state=Path(temp)/"state"; report=Path(temp)/"report.json"
            root="root-private-session-0123456789"; child="child-private-session-0123456789"
            for name, parent, times in [(root, None, [60, 18000]), (child, root, [120, 18060])]:
                meta={"session_id": name, "source": "subagent" if parent else "cli", "model": "unpriced"}
                if parent: meta["parent_session_id"]=parent
                rows=[record(0, meta, "session_meta")]+[usage(t, (i+1)*110) for i,t in enumerate(times)]
                (home/"sessions"/(name+".jsonl")).write_text("\n".join(json.dumps(r) for r in rows))
            command=[sys.executable, str(Path(profiler.__file__)), "--home", str(home), "--session", root,
                     "--no-compaction-audit", "--no-action-schema-audit", "--pause-store", str(state),
                     "--export-json", str(report)]
            first=subprocess.run(command, input="", capture_output=True, text=True, timeout=20)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertFalse(state.exists())  # ordinary profiling has no annotation writes or prompts
            original=json.loads(report.read_text()); analysis=original["pause_analysis"]
            self.assertEqual(len(analysis["quiet_intervals"]), 1)
            gap=analysis["quiet_intervals"][0]
            entry={"candidate_start": gap["start"], "candidate_end": gap["end"],
                   "start": gap["start"], "end": gap["end"], "status": "confirmed", "boundary_source": "inferred"}
            pauses.save_annotations(state, analysis["analysis_root"], [entry], None)
            second=subprocess.run(command, input="", capture_output=True, text=True, timeout=20)
            self.assertEqual(second.returncode, 0, second.stderr)
            updated=json.loads(report.read_text())
            for key in ["role_costs", "comparison_metrics", "nested_attribution", "concurrency"]:
                self.assertEqual(original[key], updated[key], key)
            self.assertEqual(analysis["elapsed"]["raw_tokens"], original["comparison_metrics"]["workflow_raw_tokens"])
            self.assertEqual(updated["pause_analysis"]["quiet_intervals"][0]["status"], "confirmed")
            self.assertGreater(updated["pause_analysis"]["excluding_confirmed_pauses"]["raw_million_tokens_per_hour"],
                               analysis["elapsed"]["raw_million_tokens_per_hour"])
            self.assertNotIn(root, report.read_text()+second.stdout)
            self.assertNotIn(child, report.read_text()+second.stdout)


if __name__ == "__main__":
    unittest.main()
