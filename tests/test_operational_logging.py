import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from mediagent.core.operational_logging import OperationLogger, ProgressLogger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "
    r"(?:DEBUG|INFO |WARN |ERROR|CRITICAL) "
    r"[a-z0-9_.-]+ .+$"
)


class OperationalLoggingTests(unittest.TestCase):
    def test_log_line_is_sanitized_single_line_without_emoji(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create("add", env={}, stream=output)

        operation_log.warning(
            "Retrying 🚀 https://user:pass@example.com/file.jpg?token=secret#fragment "
            "with Authorization: Bearer bearer-secret, password=hidden, "
            "Cookie: session=private, and /home/private/account/session.json.\nPlease wait."
        )

        line = output.getvalue().strip()
        self.assertRegex(line, LOG_LINE)
        self.assertIn("WARN  add Retrying", line)
        self.assertIn("https://example.com/file.jpg", line)
        self.assertIn("Authorization=<redacted>", line)
        self.assertIn("Cookie=<redacted>", line)
        self.assertIn("password=<redacted>", line)
        self.assertIn("<path>/session.json", line)
        self.assertNotIn("🚀", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("private", line)
        self.assertNotIn("user:pass", line)
        self.assertNotIn("/home/", line)
        self.assertNotIn("\nPlease", line)

    def test_progress_is_aggregate_and_time_throttled(self) -> None:
        output = io.StringIO()
        current = [100.0]
        operation_log = OperationLogger.create("add", env={}, stream=output)
        progress = ProgressLogger(
            operation_log,
            interval_seconds=60.0,
            clock=lambda: current[0],
        )

        self.assertTrue(progress.report(completed=1, pending=9))
        current[0] += 59.0
        self.assertFalse(progress.report(completed=2, pending=8))
        current[0] += 1.0
        self.assertTrue(progress.report(completed=3, pending=7, failed=1))

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("1 completed, 9 pending, 0 failed", lines[0])
        self.assertIn("3 completed, 7 pending, 1 failed", lines[1])

    def test_json_stdout_stays_machine_readable_while_logs_use_stderr(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "mediagent",
                "tools",
                "run",
                "core.env.check",
                "--json",
                "--input",
                "-",
            ],
            input=json.dumps({"required": []}),
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "success")
        log_lines = completed.stderr.splitlines()
        self.assertEqual(len(log_lines), 2)
        self.assertTrue(all(LOG_LINE.fullmatch(line) for line in log_lines))
        self.assertIn("core.env.check Started.", log_lines[0])
        self.assertIn("core.env.check Completed successfully", log_lines[1])

    def test_log_level_can_suppress_normal_operation_lines(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create(
            "status",
            env={"MEDIAGENT_LOG_LEVEL": "ERROR"},
            stream=output,
        )

        operation_log.started(dry_run=False)
        operation_log.completed(elapsed_seconds=0.1)

        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
