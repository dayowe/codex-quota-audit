"""Research-only throughput comparison regression tests."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cqa.research import throughput_compare as compare
from cqa import cli as cqa_cli

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def row(seconds: float, typ: str, payload: dict) -> dict:
    return {
        "timestamp": (NOW + timedelta(seconds=seconds)).isoformat(),
        "type": typ,
        "payload": payload,
    }


def epoch_ms(seconds: float) -> int:
    return int((NOW + timedelta(seconds=seconds)).timestamp() * 1000)


def token_count(seconds: float, *, inp: int, output: int, reasoning: int,
                total_inp: int, total_output: int) -> dict:
    return row(seconds, "event_msg", {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": inp,
                "cached_input_tokens": 0,
                "output_tokens": output,
                "reasoning_output_tokens": reasoning,
            },
            "total_token_usage": {
                "input_tokens": total_inp,
                "cached_input_tokens": 0,
                "output_tokens": total_output,
                "reasoning_output_tokens": reasoning,
            },
        },
    })


class TokscaleStyleParserTests(unittest.TestCase):
    def _write(self, rows: list[dict]) -> str:
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "session.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        return str(path)

    def test_non_overlapping_cursor_ignores_duplicate_zero_and_marks_tool_interval(self):
        rows = [
            row(0, "turn_context", {"model": "model-a", "reasoning_effort": "high"}),
            token_count(1, inp=100, output=15, reasoning=5, total_inp=100, total_output=15),
            # Exact duplicate cumulative snapshot: rejected and must not advance cursor.
            token_count(2, inp=100, output=15, reasoning=5, total_inp=100, total_output=15),
            # Zero-token last usage without a cumulative total: rejected, cursor stays at t=1.
            row(3, "event_msg", {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0},
            }}),
            # Tool result signal lies in the interval that closes at t=5.
            row(4, "response_item", {"type": "function_call_output", "output": "SECRET_TOOL_OUTPUT"}),
            token_count(5, inp=10, output=6, reasoning=2, total_inp=110, total_output=21),
            row(10, "turn_context", {"model": "model-a", "reasoning_effort": "high"}),
            token_count(12, inp=10, output=4, reasoning=1, total_inp=120, total_output=25),
        ]
        entries, diag = compare.parse_tokscale_style(self._write(rows))
        self.assertEqual([round(e.duration_seconds or 0, 6) for e in entries], [1.0, 4.0, 2.0])
        self.assertEqual([e.tokens.nonreasoning_output_tokens for e in entries], [10, 4, 3])
        self.assertEqual([e.first_interval for e in entries], [True, False, True])
        self.assertEqual([e.tool_result_interval for e in entries], [False, True, False])
        self.assertEqual(diag.duplicate_cumulative, 1)
        self.assertEqual(diag.zero_token_snapshot, 1)
        stats = compare._aggregate_style(entries)
        self.assertAlmostEqual(stats["output_tokens_per_second"], 17 / 7)
        self.assertAlmostEqual(
            stats["excluding_tool_result_intervals"]["output_tokens_per_second"], 13 / 3
        )
        self.assertAlmostEqual(stats["first_intervals"]["duration_share"], 3 / 7)

    def test_near_stale_cumulative_regression_is_rejected_without_advancing_cursor(self):
        rows = [
            row(0, "turn_context", {"model": "model-a"}),
            token_count(1, inp=100, output=20, reasoning=5, total_inp=100, total_output=20),
            # Slight backwards cumulative movement: treat as an out-of-order stale snapshot.
            token_count(2, inp=1, output=1, reasoning=5, total_inp=99, total_output=20),
            # Because t=2 was rejected, this interval still begins at the accepted t=1 cursor.
            token_count(4, inp=10, output=5, reasoning=0, total_inp=110, total_output=25),
        ]
        entries, diag = compare.parse_tokscale_style(self._write(rows))
        self.assertEqual(len(entries), 2)
        self.assertEqual([e.duration_seconds for e in entries], [1.0, 3.0])
        self.assertEqual(diag.stale_regression, 1)

    def test_human_user_message_resets_cursor_instead_of_bridging_idle_time(self):
        rows = [
            row(0, "turn_context", {"model": "model-a"}),
            token_count(2, inp=10, output=5, reasoning=0, total_inp=10, total_output=5),
            row(100, "event_msg", {"type": "user_message", "message": "PRIVATE_PROMPT"}),
            token_count(102, inp=10, output=5, reasoning=0, total_inp=20, total_output=10),
        ]
        entries, diag = compare.parse_tokscale_style(self._write(rows))
        self.assertEqual([e.duration_seconds for e in entries], [2.0, 2.0])
        self.assertEqual(diag.cursor_resets_user_message, 1)


class ToolExcludedResponseTests(unittest.TestCase):
    def _write(self, rows: list[dict]) -> str:
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "session.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        return str(path)

    def test_tool_wait_is_subtracted_while_reasoning_and_generation_remain(self):
        turn = "turn-private-1"
        call = "call-private-1"
        rows = [
            row(0, "event_msg", {"type": "task_started", "turn_id": turn}),
            row(0.1, "turn_context", {"turn_id": turn, "model": "model-a", "effort": "xhigh"}),
            row(1, "event_msg", {
                "type": "item_completed", "turn_id": turn,
                "started_at_ms": epoch_ms(0.5), "completed_at_ms": epoch_ms(1.0),
                "item": {"type": "Reasoning"},
            }),
            row(2, "response_item", {
                "type": "function_call", "turn_id": turn, "call_id": call,
                "name": "PRIVATE_TOOL_NAME", "arguments": "PRIVATE_ARGUMENTS",
            }),
            row(2.1, "token_usage_record", {
                "turn_id": turn,
                "usage": {"input_tokens": 100, "output_tokens": 20, "reasoning_output_tokens": 5},
            }),
            # Five seconds of external tool/wait time: excluded from the denominator.
            row(7, "response_item", {
                "type": "function_call_output", "turn_id": turn, "call_id": call,
                "output": "PRIVATE_TOOL_OUTPUT",
            }),
            row(8.5, "event_msg", {
                "type": "item_completed", "turn_id": turn,
                "started_at_ms": epoch_ms(7.5), "completed_at_ms": epoch_ms(8.5),
                "item": {"type": "Reasoning"},
            }),
            row(9.5, "event_msg", {
                "type": "item_completed", "turn_id": turn,
                "started_at_ms": epoch_ms(8.5), "completed_at_ms": epoch_ms(9.5),
                "item": {"type": "AgentMessage", "text": "PRIVATE_RESPONSE"},
            }),
            row(10, "token_usage_record", {
                "turn_id": turn,
                "usage": {"input_tokens": 100, "output_tokens": 30, "reasoning_output_tokens": 10},
            }),
            row(12, "event_msg", {
                "type": "task_complete", "turn_id": turn, "time_to_first_token_ms": 1500,
            }),
        ]
        tasks = compare.parse_tool_excluded_tasks(self._write(rows))
        self.assertEqual(len(tasks), 1)
        stats = compare._aggregate_tool_excluded(tasks)
        # 12s observed task span - 5s paired tool span = 7s model/client-active time.
        self.assertAlmostEqual(stats["task_elapsed_seconds"], 12.0)
        self.assertAlmostEqual(stats["tool_wait_seconds"], 5.0)
        self.assertAlmostEqual(stats["tool_excluded_seconds"], 7.0)
        # Non-reasoning model output includes tool-call output tokens: (20-5)+(30-10)=35.
        self.assertAlmostEqual(stats["output_tokens_per_second"], 35 / 7)
        self.assertEqual(stats["exact_visible_output_tokens"], 20)
        self.assertAlmostEqual(stats["timed_reasoning_seconds"], 1.5)
        self.assertAlmostEqual(stats["timed_visible_generation_seconds"], 1.0)
        self.assertAlmostEqual(stats["other_tool_excluded_seconds"], 4.5)
        self.assertEqual(stats["tool_pairing_coverage"], 1.0)
        self.assertEqual(stats["evidence_quality"], "exact")
        self.assertAlmostEqual(stats["ttft_p50_seconds"], 1.5)
        serialized = json.dumps(stats)
        for canary in (call, "PRIVATE_TOOL_NAME", "PRIVATE_ARGUMENTS", "PRIVATE_TOOL_OUTPUT", "PRIVATE_RESPONSE"):
            self.assertNotIn(canary, serialized)

    def test_unpaired_tool_call_is_not_guessed_and_invalid_reasoning_is_reported(self):
        turn = "turn-private-2"
        rows = [
            row(0, "event_msg", {"type": "task_started", "turn_id": turn}),
            row(0.1, "turn_context", {"turn_id": turn, "model": "model-a", "effort": "high"}),
            row(1, "event_msg", {
                "type": "item_completed", "turn_id": turn,
                "started_at_ms": epoch_ms(2), "completed_at_ms": epoch_ms(1),
                "item": {"type": "Reasoning"},
            }),
            row(2, "response_item", {
                "type": "custom_tool_call", "turn_id": turn, "call_id": "unpaired-secret",
                "name": "exec", "input": "SECRET_COMMAND",
            }),
            row(2.1, "token_usage_record", {
                "turn_id": turn,
                "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 2},
            }),
            row(4, "event_msg", {"type": "task_complete", "turn_id": turn}),
        ]
        stats = compare._aggregate_tool_excluded(compare.parse_tool_excluded_tasks(self._write(rows)))
        self.assertEqual(stats["complete_tasks"], 1)
        self.assertEqual(stats["qualified_tasks"], 0)
        self.assertEqual(stats["unpaired_tool_calls"], 1)
        self.assertEqual(stats["invalid_reasoning_items"], 1)
        self.assertIsNone(stats["output_tokens_per_second"])
        self.assertEqual(stats["evidence_quality"], "unavailable")

    def test_tokscale_exact_command_uses_codex_home_not_tokscale_home_flag(self):
        cmd = compare.tokscale_exact_command("/tmp/cqa tokscale")
        self.assertIn("CODEX_HOME='/tmp/cqa tokscale'", cmd)
        self.assertNotIn("--home", cmd)
        self.assertIn("--client codex", cmd)

    def test_activation_filter_keeps_pre_activation_model_context_only(self):
        turn = "turn-after-activation"
        rows = [
            row(0, "session_meta", {"model": "model-pre"}),
            row(0.1, "turn_context", {"turn_id": "inherited", "model": "model-a", "effort": "xhigh"}),
            row(2, "event_msg", {"type": "task_started", "turn_id": turn}),
            row(3, "token_usage_record", {
                "turn_id": turn,
                "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 1},
            }),
            row(4, "event_msg", {"type": "task_complete", "turn_id": turn}),
        ]
        tasks = compare.parse_tool_excluded_tasks(
            self._write(rows), activation=NOW + timedelta(seconds=1)
        )
        complete = [t for t in tasks if t.start_ts and t.end_ts]
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0].model, "model-a")
        self.assertEqual(complete[0].effort, "xhigh")


class ThroughputComparisonIntegrationTests(unittest.TestCase):
    def test_comparison_is_privacy_safe_and_preserves_distinct_semantics(self):
        secret_session = "raw-session-secret-123456789"
        secret_prompt = "PROMPT_CANARY_DO_NOT_EXPORT_8c42"
        secret_tool = "TOOL_CANARY_DO_NOT_EXPORT_7e19"
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            logs = home / "sessions" / "2026" / "10" / "01"
            logs.mkdir(parents=True)
            path = logs / "rollout-019dbb7a-0000-7000-8000-000000000001.jsonl"
            rows = [
                row(0, "session_meta", {"session_id": secret_session, "source": "cli", "model": "model-a"}),
                row(1, "turn_context", {"model": "model-a", "reasoning_effort": "high"}),
                row(1.1, "event_msg", {"type": "user_message", "message": secret_prompt}),
                row(3, "event_msg", {
                    "type": "item_completed", "turn_id": "turn-private-123456",
                    "started_at_ms": 1000, "completed_at_ms": 2000,
                    "item": {"type": "AgentMessage", "text": "PRIVATE_RESPONSE"},
                }),
                row(4, "token_usage_record", {
                    "type": "token_usage_record", "turn_id": "turn-private-123456",
                    "usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 30, "reasoning_output_tokens": 10,
                    },
                    "last_token_usage": {
                        "input_tokens": 100, "cached_input_tokens": 0,
                        "output_tokens": 30, "reasoning_output_tokens": 10,
                    },
                    "total_token_usage": {"total_tokens": 130},
                }),
                row(4.5, "response_item", {"type": "function_call_output", "output": secret_tool}),
                token_count(5, inp=100, output=30, reasoning=10, total_inp=100, total_output=30),
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            cli_json = home / "cli-comparison.json"
            cli_csv = home / "cli-comparison.csv"
            rc = cqa_cli.main([
                "research", "throughput-compare", "latest", "--home", str(home),
                "--json", str(cli_json), "--csv", str(cli_csv),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(cli_json.is_file())
            self.assertTrue(cli_csv.is_file())

            report = compare.build_comparison(str(home), "latest")
            overall = next(r for r in report["rows"] if r["scope"] == "overall")
            # CQA uses the 1s AgentMessage interval: 20 visible tokens / 1s.
            self.assertAlmostEqual(overall["cqa_visible"]["visible_output_tokens_per_second"], 20.0)
            # Tokscale-style uses the client accounting cursor; the human user message reset is t=1.1 -> t=5.
            self.assertAlmostEqual(overall["tokscale_style"]["output_tokens_per_second"], 20 / 3.9)
            self.assertLess(overall["difference"]["percent_vs_cqa"], 0)
            self.assertEqual(report["schema"], "cqa-throughput-comparison")
            self.assertEqual(report["privacy"]["contains_raw_session_ids"], False)

            jp = home / "comparison.json"
            cp = home / "comparison.csv"
            compare.write_json(report, jp)
            compare.write_csv(report, cp)
            combined = jp.read_text(encoding="utf-8") + cp.read_text(encoding="utf-8")
            for canary in (secret_session, secret_prompt, secret_tool, str(path)):
                self.assertNotIn(canary, combined)
            self.assertIn("session-001", combined)
            self.assertIn("Tokscale-style reconstruction", combined)

    def test_stage_tokscale_home_contains_exact_selected_population_and_is_rebuildable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "codex-home"
            logs = home / "sessions" / "2026" / "10" / "01"
            logs.mkdir(parents=True)
            rollout = logs / "rollout-019dbb7a-0000-7000-8000-000000000777.jsonl"
            rollout.write_text("\n".join(json.dumps(r) for r in [
                row(0, "session_meta", {"session_id": "RAW-PRIVATE-ID-777", "source": "cli", "model": "model-a"}),
                row(1, "turn_context", {"model": "model-a", "reasoning_effort": "high"}),
                token_count(2, inp=10, output=5, reasoning=0, total_inp=10, total_output=5),
            ]) + "\n", encoding="utf-8")

            resolved_home, family, sessions = compare._resolve_selection(str(home), "latest", 90.0)
            stage_home = root / "tokscale-stage"
            info = compare.stage_tokscale_home(resolved_home, family, sessions, stage_home)
            self.assertEqual(info["sessions"], 1)
            staged = list((stage_home / "sessions").rglob("*.jsonl"))
            self.assertEqual(len(staged), 1)
            self.assertEqual(staged[0].read_bytes(), rollout.read_bytes())
            marker = json.loads((stage_home / compare.STAGE_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["kind"], "cqa-tokscale-stage")
            self.assertEqual(marker["selected_sessions"], 1)
            self.assertNotIn(str(rollout), json.dumps(marker))
            self.assertNotIn("RAW-PRIVATE-ID-777", json.dumps(marker))

            # Rebuilding an owned stage replaces only the staged sessions and keeps
            # unrelated upstream output the user may have written beside it.
            exact_output = stage_home / "tokscale-exact.json"
            exact_output.write_text("{}\n", encoding="utf-8")
            compare.stage_tokscale_home(resolved_home, family, sessions, stage_home)
            self.assertTrue(exact_output.exists())
            self.assertEqual(len(list((stage_home / "sessions").rglob("*.jsonl"))), 1)

    def test_cli_stage_option_builds_tokscale_home(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "codex-home"
            logs = home / "sessions" / "2026" / "10" / "01"
            logs.mkdir(parents=True)
            rollout = logs / "rollout-019dbb7a-0000-7000-8000-000000000778.jsonl"
            rollout.write_text("\n".join(json.dumps(r) for r in [
                row(0, "session_meta", {"session_id": "RAW-PRIVATE-ID-778", "source": "cli", "model": "model-a"}),
                row(1, "turn_context", {"model": "model-a"}),
                token_count(2, inp=10, output=5, reasoning=0, total_inp=10, total_output=5),
            ]) + "\n", encoding="utf-8")
            stage_home = root / "stage"
            out_json = root / "comparison.json"
            rc = cqa_cli.main([
                "research", "throughput-compare", "latest",
                "--home", str(home),
                "--json", str(out_json), "--no-csv",
                "--stage-tokscale-home", str(stage_home),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(out_json.is_file())
            self.assertTrue((stage_home / compare.STAGE_MARKER).is_file())
            self.assertEqual(len(list((stage_home / "sessions").rglob("*.jsonl"))), 1)

    def test_stage_refuses_nonempty_unowned_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source_home = root / "codex-home"
            source_home.mkdir()
            stage_home = root / "stage"
            stage_home.mkdir()
            (stage_home / "keep.txt").write_text("do not delete", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not empty"):
                compare._prepare_stage_root(str(source_home), stage_home)
            self.assertEqual((stage_home / "keep.txt").read_text(encoding="utf-8"), "do not delete")


if __name__ == "__main__":
    unittest.main()
