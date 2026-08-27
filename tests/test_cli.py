import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core import db, library_content


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_tools_list_json(self) -> None:
        completed = self.run_cli("tools", "list", "--json")

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertIn("tools", payload)
        self.assertIn("core.env.check", {tool["name"] for tool in payload["tools"]})
        self.assertNotIn("link.resolve.preview", {tool["name"] for tool in payload["tools"]})
        self.assertNotIn("telegram.inbox.sync_links", {tool["name"] for tool in payload["tools"]})

    def test_tools_list_can_include_experimental_tools_explicitly(self) -> None:
        completed = self.run_cli("tools", "list", "--json", "--include-experimental")

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertIn("link.resolve.preview", {tool["name"] for tool in payload["tools"]})
        self.assertNotIn("telegram.inbox.sync_links", {tool["name"] for tool in payload["tools"]})

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

    def test_tools_run_summary_json_omits_verbose_tool_data(self) -> None:
        completed = self.run_cli(
            "tools",
            "run",
            "core.env.check",
            "--summary-json",
            "--input",
            "-",
            input_text=json.dumps({"required": []}),
        )

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["tool"], "core.env.check")
        self.assertEqual(payload["data"], {})
        self.assertEqual(payload["artifact_count"], 0)

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

    def test_public_link_sync_recognizes_comic_links(self) -> None:
        from mediagent.cli import _is_comic_link

        self.assertTrue(_is_comic_link("https://nhentai.net/g/513148/"))
        self.assertTrue(_is_comic_link("https://18comic.vip/album/624076/?series_sort=1"))
        self.assertTrue(_is_comic_link("https://18comic.vip/photo/1459311/"))
        self.assertFalse(_is_comic_link("https://example.com/file.jpg"))

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

    def test_library_cli_deduplicate_rename_remove_restore_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            library_root = Path(temp_dir) / "library"
            db_path = data_dir / "mediagent.sqlite3"
            source = library_root / "photo/original.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"cli-library-content")
            db.initialize_database(db_path)
            db.upsert_media_item(
                db_path,
                {"platform": "pixiv", "remote_id": "cli-one", "media_type": "photo"},
            )
            file_record = db.upsert_media_file(
                db_path,
                platform="pixiv",
                remote_id="cli-one",
                remote_url="https://example.invalid/cli-one.jpg",
                local_path=str(source),
                mime_type="image/jpeg",
                size_bytes=source.stat().st_size,
                checksum=library_content.sha256_checksum(source)[0],
                status="downloaded",
                library_relative_path="photo/original.jpg",
            )
            library_content.adopt_media_file(db_path, file_id=file_record["id"])
            env_updates = {
                "MEDIAGENT_DATA_DIR": str(data_dir),
                "MEDIAGENT_LIBRARY_DIR": str(library_root),
                "MEDIAGENT_DB_PATH": str(db_path),
            }

            preview = self.run_cli(
                "library",
                "deduplicate",
                "--dry-run",
                "--json",
                env_updates=env_updates,
            )
            renamed = self.run_cli(
                "library",
                "rename",
                "--path",
                str(source),
                "--name",
                "renamed",
                "--json",
                env_updates=env_updates,
            )
            renamed_payload = json.loads(renamed.stdout)
            renamed_path = Path(renamed_payload["data"]["new_path"])
            removed = self.run_cli(
                "library",
                "remove",
                "--path",
                str(renamed_path),
                "--reason",
                "cli test",
                "--json",
                env_updates=env_updates,
            )
            removed_payload = json.loads(removed.stdout)
            restored = self.run_cli(
                "library",
                "restore",
                "--removal-id",
                removed_payload["data"]["removal_id"],
                "--json",
                env_updates=env_updates,
            )

            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertTrue(json.loads(preview.stdout)["data"]["dry_run"])
            self.assertEqual(renamed.returncode, 0, renamed.stderr)
            self.assertTrue(renamed_path.is_file())
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertTrue(renamed_path.is_file())

    def test_library_cli_reconcile_trash_dry_run(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            library_root = Path(temp_dir) / "library"
            db_path = data_dir / "mediagent.sqlite3"
            source = library_root / "pixiv/photo/legacy.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"cli-legacy-trash")
            db.initialize_database(db_path)
            db.upsert_media_item(
                db_path,
                {"platform": "pixiv", "remote_id": "cli-legacy", "media_type": "photo"},
            )
            db.upsert_media_file(
                db_path,
                platform="pixiv",
                remote_id="cli-legacy",
                remote_url="https://example.invalid/cli-legacy.jpg",
                local_path=str(source),
                mime_type="image/jpeg",
                size_bytes=source.stat().st_size,
                checksum=library_content.sha256_checksum(source)[0],
                status="downloaded",
                library_relative_path="pixiv/photo/legacy.jpg",
            )
            trash = library_root / ".trash/2026-08-27/pixiv/photo/legacy.jpg"
            trash.parent.mkdir(parents=True)
            os.replace(source, trash)
            completed = self.run_cli(
                "library",
                "reconcile-trash",
                "--dry-run",
                "--json",
                env_updates={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_LIBRARY_DIR": str(library_root),
                    "MEDIAGENT_DB_PATH": str(db_path),
                },
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["tool"], "library.trash.reconcile")
            self.assertEqual(payload["data"]["plan"]["summary"]["source_rows_importable"], 1)
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM library_entries").fetchone()[0], 0)

    def run_cli(
        self,
        *args: str,
        input_text: str | None = None,
        env_updates: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        env.update(env_updates or {})
        return subprocess.run(
            [sys.executable, "-m", "mediagent", *args],
            input=input_text,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            env=env,
            check=False,
        )
