"""Regression tests for the bounded, diagnostic release runner."""
from __future__ import annotations

import contextlib
import io
import sys
import time
import unittest

from tools.release_check import Runner


class ReleaseCheckRunnerTests(unittest.TestCase):
    def test_runner_reports_stage_before_success(self):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            Runner(1).run("fast smoke", [sys.executable, "-c", "print('ok')"], timeout=2)
        text = out.getvalue()
        self.assertIn("[01/01] fast smoke", text)
        self.assertIn("OK (", text)
        self.assertEqual(err.getvalue(), "")

    def test_runner_times_out_with_actionable_diagnostics(self):
        out = io.StringIO()
        err = io.StringIO()
        started = time.monotonic()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                Runner(1).run(
                    "slow smoke",
                    [sys.executable, "-c", "import time; time.sleep(2)"],
                    timeout=0.05,
                )
        elapsed = time.monotonic() - started
        self.assertEqual(cm.exception.code, 124)
        self.assertLess(elapsed, 1.5)
        self.assertIn("TIMEOUT", out.getvalue())
        self.assertIn("Command:", err.getvalue())
        self.assertIn("Timeout: 0.05s", err.getvalue())


if __name__ == "__main__":
    unittest.main()
