import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples" / "tools"


class CliSmokeTests(unittest.TestCase):
    def test_bottom_tool_examples_are_valid_json(self) -> None:
        for path in EXAMPLES.glob("*.json"):
            with self.subTest(path=path.name):
                json.loads(path.read_text(encoding="utf-8"))

    def test_bottom_tool_examples_smoke(self) -> None:
        tools = [
            "core.env.check",
            "core.db.init",
            "core.path.prepare",
            "core.run.record",
            "core.sync_cursor.set",
            "core.sync_cursor.get",
            "media.item.upsert",
            "media.item.filter_new",
            "media.item.set_status",
            "media.file.upsert",
            "storage.path.plan",
            "download.http",
            "metadata.write",
            "telegram.auth.login",
            "telegram.auth.status",
            "telegram.dialogs.list",
            "telegram.messages.collect",
            "telegram.media.download",
            "telegram.messages.sync",
        ]
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
            env["MEDIAGENT_DATA_DIR"] = str(data_dir)
            env["MEDIAGENT_DB_PATH"] = str(db_path)
            data_dir.mkdir()

            for tool in tools:
                input_path = EXAMPLES / f"{tool}.json"
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "mediagent",
                        "tools",
                        "run",
                        tool,
                        "--input",
                        str(input_path),
                        "--dry-run",
                        "--json",
                    ],
                    cwd=PROJECT_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                with self.subTest(tool=tool):
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    self.assertEqual(json.loads(completed.stdout)["status"], "success")
