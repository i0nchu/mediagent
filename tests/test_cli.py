import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_tools_list_json(self) -> None:
        completed = self.run_cli("tools", "list", "--json")

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertIn("tools", payload)
        self.assertIn("core.env.check", {tool["name"] for tool in payload["tools"]})
        self.assertNotIn("link.resolve.preview", {tool["name"] for tool in payload["tools"]})

    def test_tools_list_can_include_experimental_tools_explicitly(self) -> None:
        completed = self.run_cli("tools", "list", "--json", "--include-experimental")

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertIn("link.resolve.preview", {tool["name"] for tool in payload["tools"]})
        self.assertIn("telegram.inbox.sync_links", {tool["name"] for tool in payload["tools"]})

    def test_tools_inspect_rejects_experimental_without_allow_flag(self) -> None:
        completed = self.run_cli("tools", "inspect", "link.resolve.preview", "--json")

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"]["code"], "experimental_tool_not_allowed")

    def test_tools_run_rejects_experimental_without_allow_flag(self) -> None:
        completed = self.run_cli(
            "tools",
            "run",
            "link.resolve.to_media_item",
            "--json",
            "--dry-run",
            "--input",
            "-",
            input_text=json.dumps({"resolution": {"status": "skipped", "skip_reason": "manual_test"}}),
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"]["code"], "experimental_tool_not_allowed")

    def test_top_level_help_does_not_expose_experimental_command(self) -> None:
        completed = self.run_cli("--help")

        self.assertEqual(completed.returncode, 0)
        self.assertNotIn("experimental", completed.stdout)
        self.assertNotIn("==SUPPRESS==", completed.stdout)

    def test_tools_inspect_unknown_exits_with_validation_error(self) -> None:
        completed = self.run_cli("tools", "inspect", "missing.tool", "--json")

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"]["code"], "unknown_tool")

    def test_tools_run_json_success(self) -> None:
        completed = self.run_cli(
            "tools",
            "run",
            "core.env.check",
            "--json",
            input_text=json.dumps({"required": []}),
        )

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["tool"], "core.env.check")

    def test_public_link_sync_entrypoint_uses_link_media_sync(self) -> None:
        with TemporaryDirectory() as temp_dir:
            completed = self.run_cli(
                "link",
                "sync",
                "https://127.0.0.1/file.jpg",
                "--db-path",
                str(Path(temp_dir) / "mediagent.sqlite3"),
                "--dry-run",
                "--json",
            )

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["tool"], "link.media.sync")
        self.assertEqual(payload["data"]["summary"]["links_considered"], 1)
        self.assertEqual(payload["data"]["summary"]["skipped_links"], 1)
        self.assertEqual(payload["data"]["links"][0]["resolution"]["skip_reason"], "unsafe_url")

    def test_tools_run_json_runtime_failure(self) -> None:
        completed = self.run_cli(
            "tools",
            "run",
            "core.env.check",
            "--json",
            "--input",
            "-",
            input_text=json.dumps({"required": ["MEDIAGENT_NOT_SET"]}),
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"]["code"], "env_check_failed")

    def test_tools_run_reads_input_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.json"
            input_path.write_text(json.dumps({"required": []}), encoding="utf-8")

            completed = self.run_cli(
                "tools",
                "run",
                "core.env.check",
                "--json",
                "--input",
                str(input_path),
            )

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "success")

    def run_cli(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        return subprocess.run(
            [sys.executable, "-m", "mediagent", *args],
            input=input_text,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env,
            check=False,
        )
