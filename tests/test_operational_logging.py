import io
import asyncio
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent import cli
from mediagent.core.operational_logging import OperationLogger, ProgressLogger, await_with_heartbeat


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
        self.assertNotIn("🚀", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("private", line)
        self.assertNotIn("user:pass", line)
        self.assertNotIn("/home/", line)
        self.assertNotIn("session.json", line)
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

        self.assertFalse(progress.report(completed=1, pending=9))
        current[0] += 59.0
        self.assertFalse(progress.report(completed=2, pending=8))
        current[0] += 1.0
        self.assertTrue(progress.report(completed=3, pending=7, failed=1))
        current[0] += 1.0
        self.assertTrue(progress.report(completed=4, pending=6, failed=1, force=True))

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("3 completed, 7 pending, 1 failed, 1m 0s elapsed", lines[0])
        self.assertIn("4 completed, 6 pending, 1 failed, 1m 1s elapsed", lines[1])

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

    def test_invalid_log_level_falls_back_to_normal_info_output(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create(
            "status",
            env={"MEDIAGENT_LOG_LEVEL": "not-a-level"},
            stream=output,
        )

        operation_log.info("Configuration is ready.")

        self.assertIn("INFO  status Configuration is ready.", output.getvalue())

    def test_cli_does_not_create_a_log_file_by_default_or_from_legacy_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "mediagent.log"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
            env["MEDIAGENT_LOG_PATH"] = str(log_path)
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
            self.assertFalse(log_path.exists())

    def test_tool_warnings_are_bounded_and_use_the_sanitized_logger(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create("sync", env={}, stream=output)

        cli.log_tool_warnings(
            operation_log,
            [
                "First warning with token=secret.",
                "Second warning 🚀.",
                "Third warning.",
                "Fourth warning.",
                "Fifth warning.",
            ],
        )

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(all("WARN  sync" in line for line in lines))
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("🚀", output.getvalue())
        self.assertIn("2 additional warnings were omitted", lines[-1])

    def test_adversarial_headers_paths_and_unicode_are_removed(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create("download", env={}, stream=output)

        operation_log.error(
            "headers={'Authorization': 'Bearer SUPERSECRET', 'Cookie': 'identity=ALPHA; preference=BRAVO'} "
            "fallback Bearer STANDALONESECRET; "
            "paths /home/private/My Secret Folder/file name.jpg, "
            r"C:\Users\Private User\session.json, \\server\private\cookie.txt, "
            "file:///home/private/credential.json and spoof \u202Etext 1\uFE0F\u20E3."
        )

        rendered = output.getvalue()
        for secret in (
            "SUPERSECRET",
            "ALPHA",
            "BRAVO",
            "STANDALONESECRET",
            "/home/private",
            "Private User",
            "server\\private",
            "credential.json",
            "\u202E",
            "\u20E3",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("'Authorization': '<redacted>'", rendered)
        self.assertIn("'Cookie': '<redacted>'", rendered)
        self.assertIn("Bearer <redacted>", rendered)

    def test_long_silent_operation_emits_heartbeat_without_item_events(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create("download", env={}, stream=output)

        async def slow_operation() -> str:
            await asyncio.sleep(0.035)
            return "complete"

        result = asyncio.run(
            await_with_heartbeat(
                slow_operation(),
                operation_log,
                interval_seconds=0.01,
            )
        )

        self.assertEqual(result, "complete")
        lines = output.getvalue().splitlines()
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(all("Still working;" in line for line in lines))
        self.assertTrue(all("elapsed." in line for line in lines))

    def test_recent_provider_progress_suppresses_redundant_heartbeat(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create("sync", env={}, stream=output)

        async def operation_with_progress() -> None:
            for completed in range(1, 4):
                await asyncio.sleep(0.006)
                operation_log.info(f"Progress: {completed} completed.")

        asyncio.run(
            await_with_heartbeat(
                operation_with_progress(),
                operation_log,
                interval_seconds=0.01,
            )
        )

        rendered = output.getvalue()
        self.assertEqual(rendered.count("Progress:"), 3)
        self.assertNotIn("Still working;", rendered)

    def test_disabled_info_logging_does_not_busy_loop_heartbeat(self) -> None:
        output = io.StringIO()
        operation_log = OperationLogger.create(
            "download",
            env={"MEDIAGENT_LOG_LEVEL": "ERROR"},
            stream=output,
        )
        heartbeat_attempts = 0
        original_info = operation_log.info

        def counted_info(message: str) -> None:
            nonlocal heartbeat_attempts
            heartbeat_attempts += 1
            original_info(message)

        operation_log.info = counted_info  # type: ignore[method-assign]

        async def slow_operation() -> None:
            await asyncio.sleep(0.035)

        asyncio.run(
            await_with_heartbeat(
                slow_operation(),
                operation_log,
                interval_seconds=0.01,
            )
        )

        self.assertLessEqual(heartbeat_attempts, 4)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
