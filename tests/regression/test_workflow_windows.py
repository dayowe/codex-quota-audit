"""Task-window boundaries retain accounting without inventing partial rates."""
import json
import unittest
from datetime import timedelta

from tests.regression.test_generic_workflow import record
from tests.regression.test_workflow_attribution import NOW
from cqa.workflow import response_efficiency


class TaskWindowTests(unittest.TestCase):
    def parse(self, rows, **kwargs):
        return response_efficiency.parse_tool_excluded_tasks(
            "unused", records=(json.dumps(r).encode() for r in rows), **kwargs)

    def task(self, name, start, response, end):
        return [record(start, {"type": "task_started", "turn_id": name}, "event_msg"),
                record(response, {"turn_id": name, "usage": {"output_tokens": 20, "reasoning_output_tokens": 5}},
                       "token_usage_record"),
                record(end, {"type": "task_complete", "turn_id": name}, "event_msg")]

    def test_complete_and_crossing_tasks_have_separate_evidence(self):
        rows = [record(0, {"model": "model-a", "effort": "xhigh"}, "turn_context")]
        rows += self.task("carry-in", 1, 6, 7)
        rows += self.task("complete", 5, 8, 9)
        rows += self.task("carry-out", 10, 11, 12)
        rows += [record(8, {"type": "task_started", "turn_id": "open"}, "event_msg"),
                 record(9, {"turn_id": "no-start", "usage": {"output_tokens": 999}}, "token_usage_record")]
        tasks = self.parse(sorted(rows, key=lambda r: r["timestamp"]),
                           after=NOW + timedelta(seconds=5), before=NOW + timedelta(seconds=12))
        stats = response_efficiency.aggregate_tool_excluded(tasks)
        self.assertEqual(stats["tasks_seen"], 5)
        self.assertEqual(stats["complete_tasks"], 1)
        self.assertEqual(stats["qualified_tasks"], 1)
        self.assertEqual(stats["evidence_quality"], "partial")
        self.assertEqual(stats["nonreasoning_output_tokens"], 15)
        self.assertAlmostEqual(stats["output_tokens_per_second"], 15 / 4)
        self.assertEqual(stats["qualification_reasons"], {
            "started_before_window": 1, "completed_after_window": 1,
            "missing_task_complete": 1, "missing_task_start": 1, "qualified_task": 1})
        self.assertTrue(all(t.model == "model-a" and t.effort == "xhigh" for t in tasks))

    def test_completion_at_exclusive_end_does_not_qualify(self):
        rows = self.task("end-boundary", 1, 2, 3)
        stats = response_efficiency.aggregate_tool_excluded(self.parse(rows, before=NOW + timedelta(seconds=3)))
        self.assertEqual(stats["complete_tasks"], 0)
        self.assertIsNone(stats["output_tokens_per_second"])
        self.assertEqual(stats["qualification_reasons"], {"completed_after_window": 1})
        full = response_efficiency.aggregate_tool_excluded(self.parse(rows))
        self.assertEqual(full["qualified_tasks"], 1)

    def test_no_window_activity_does_not_create_task_evidence(self):
        rows = [record(1, {"type": "task_started", "turn_id": "silent"}, "event_msg"),
                record(20, {"type": "task_complete", "turn_id": "silent"}, "event_msg")]
        self.assertEqual(self.parse(rows, after=NOW + timedelta(seconds=5), before=NOW + timedelta(seconds=10)), [])

    def test_pre_activation_start_is_missing_evidence_not_carry_in(self):
        rows = self.task("inherited-start", 1, 6, 7)
        stats = response_efficiency.aggregate_tool_excluded(self.parse(
            rows, activation=NOW + timedelta(seconds=4), after=NOW + timedelta(seconds=5)))
        self.assertEqual(stats["qualification_reasons"], {"missing_task_start": 1})
        self.assertIsNone(stats["output_tokens_per_second"])


if __name__ == "__main__":
    unittest.main()
