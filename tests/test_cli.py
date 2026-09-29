import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mediagent import cli
from mediagent.core import db, library_content
from mediagent.core.tooling import ErrorCategory, ToolResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_human_output_removes_terminal_control_characters(self) -> None:
        self.assertEqual(
            cli._one_line("safe\x1b[2J\u202eINJECT\nnext"),
            "safe [2JINJECT next",
        )
        self.assertEqual(cli._one_line("safe\ud800text"), "safetext")

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
        self.assertEqual(payload["tool"], "link.resolve.to_media_item")
        self.assertTrue(payload["run_id"])
        self.assertEqual(payload["status"], "failure")
        self.assertEqual(payload["data"], {})
        self.assertIn("ERROR link.resolve.to_media_item Failed", completed.stderr)

    def test_top_level_help_does_not_expose_experimental_command(self) -> None:
        completed = self.run_cli("--help")

        self.assertEqual(completed.returncode, 0)
        self.assertNotIn("experimental", completed.stdout)
        self.assertNotIn("==SUPPRESS==", completed.stdout)
        self.assertIn("init", completed.stdout)
        self.assertIn("add", completed.stdout)
        self.assertIn("sync", completed.stdout)
        self.assertIn("status", completed.stdout)
        self.assertIn("remove", completed.stdout)
        self.assertIn("restore", completed.stdout)
        self.assertIn("search", completed.stdout)
        self.assertIn("tag", completed.stdout)
        self.assertIn("untag", completed.stdout)
        self.assertIn("trash", completed.stdout)

    @patch("mediagent.cli.run_tool_command", return_value=0)
    def test_short_tag_and_untag_route_asset_tools(self, run_tool) -> None:
        self.assertEqual(cli.run(["tag", "asset_example", "favorite", "blue"]), 0)
        first = run_tool.call_args
        self.assertEqual(first.kwargs["tool"], "library.asset.tags.update")
        self.assertEqual(
            first.kwargs["input_data"],
            {"asset_id": "asset_example", "add": ["favorite", "blue"]},
        )

        self.assertEqual(cli.run(["untag", "asset_example", "blue"]), 0)
        second = run_tool.call_args
        self.assertEqual(second.kwargs["tool"], "library.asset.tags.update")
        self.assertEqual(
            second.kwargs["input_data"],
            {"asset_id": "asset_example", "remove": ["blue"]},
        )

    @patch("mediagent.cli.run_tool_command", return_value=0)
    def test_short_tag_without_manual_tags_runs_automatic_tagging(self, run_tool) -> None:
        self.assertEqual(cli.run(["tag", "asset_example"]), 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "library.asset.tags.auto")
        self.assertEqual(
            run_tool.call_args.kwargs["input_data"],
            {"asset_ids": ["asset_example"], "force": True},
        )

        self.assertEqual(cli.run(["tag"]), 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "library.asset.tags.auto")
        self.assertEqual(run_tool.call_args.kwargs["input_data"], {})

    @patch("mediagent.cli.run_tool_command", return_value=0)
    def test_short_search_routes_asset_search(self, run_tool) -> None:
        self.assertEqual(cli.run(["search", "favorite", "Alice", "--all", "--limit", "12"]), 0)

        self.assertEqual(run_tool.call_args.kwargs["tool"], "library.asset.search")
        self.assertEqual(
            run_tool.call_args.kwargs["input_data"],
            {"terms": ["favorite", "Alice"], "include_inactive": True, "limit": 12},
        )

    def test_short_init_uses_configured_database(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["init", "--dry-run"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "core.db.init")
        self.assertTrue(run_tool.call_args.kwargs["dry_run"])
        self.assertTrue(run_tool.call_args.kwargs["compact_human"])

    def test_short_add_uses_smart_repair_defaults(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["add", "https://example.com/file.jpg", "--dry-run"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "media.add")
        self.assertEqual(
            run_tool.call_args.kwargs["input_data"],
            {
                "input": "https://example.com/file.jpg",
                "overwrite": False,
                "repair": True,
            },
        )
        self.assertTrue(run_tool.call_args.kwargs["compact_human"])

    def test_short_add_routes_local_input_to_copy_only_import(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["add", "./incoming", "--dry-run"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "media.add")
        self.assertEqual(
            run_tool.call_args.kwargs["input_data"],
            {"input": "./incoming", "overwrite": False, "repair": True},
        )
        self.assertTrue(run_tool.call_args.kwargs["dry_run"])
        self.assertTrue(run_tool.call_args.kwargs["compact_human"])

    def test_short_remove_routes_asset_identity(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["remove", "asset_example", "--reason", "not wanted"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "library.asset.remove")
        self.assertEqual(
            run_tool.call_args.kwargs["input_data"],
            {"asset_id": "asset_example", "reason": "not wanted"},
        )
        self.assertTrue(run_tool.call_args.kwargs["compact_human"])

    def test_short_restore_routes_asset_identity(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["restore", "asset_example"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "library.asset.restore")
        self.assertEqual(run_tool.call_args.kwargs["input_data"], {"asset_id": "asset_example"})

    def test_short_trash_purge_routes_configured_retention(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["trash", "purge", "--dry-run"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "library.trash.purge")
        self.assertEqual(run_tool.call_args.kwargs["input_data"], {})
        self.assertTrue(run_tool.call_args.kwargs["dry_run"])

    def test_short_source_sync_applies_provider_defaults(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["sync", "pixiv", "--full", "--summary-json"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "pixiv.bookmarks.sync")
        self.assertEqual(
            run_tool.call_args.kwargs["input_data"],
            {
                "overwrite": False,
                "retry_failed": True,
                "repair_missing_files": True,
                "full_sync": True,
                "package_comics": True,
                "include_ugoira_metadata": True,
            },
        )
        self.assertTrue(run_tool.call_args.kwargs["summary_json"])

    def test_short_telegram_sync_routes_unified_inbox(self) -> None:
        with (
            patch.dict(os.environ, {"MEDIAGENT_ENV_FILE": ""}),
            patch("mediagent.cli.run_tool_command", return_value=0) as run_tool,
        ):
            result = cli.run(["sync", "telegram", "--full"])

        self.assertEqual(result, 0)
        self.assertEqual(run_tool.call_args.kwargs["tool"], "telegram.inbox.sync")
        self.assertEqual(
            run_tool.call_args.kwargs["input_data"],
            {
                "overwrite": False,
                "retry_failed": True,
                "repair_missing_files": True,
                "full_sync": True,
            },
        )

    def test_short_sync_rejects_folder_for_non_jmcomic_source(self) -> None:
        completed = self.run_cli(
            "sync",
            "pixiv",
            "--folder",
            "favorites",
            "--json",
            env_updates={"MEDIAGENT_ENV_FILE": ""},
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            json.loads(completed.stdout)["error"]["code"],
            "unsupported_source_option",
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["tool"], "pixiv.bookmarks.sync")
        self.assertTrue(payload["run_id"])
        self.assertEqual(payload["data"], {})
        self.assertEqual(payload["warnings"], [])
        self.assertIn("ERROR pixiv.bookmarks.sync Failed", completed.stderr)

    def test_invalid_env_file_uses_standard_failure_boundary(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text("INVALID ENV LINE\n", encoding="utf-8")
            completed = self.run_cli(
                "status",
                "--json",
                env_updates={"MEDIAGENT_ENV_FILE": str(env_file)},
            )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["tool"], "status")
        self.assertTrue(payload["run_id"])
        self.assertEqual(payload["status"], "failure")
        self.assertEqual(payload["data"], {})
        self.assertEqual(payload["artifacts"], [])
        self.assertEqual(payload["warnings"], [])
        self.assertIsNone(payload["rate_limit"])
        self.assertEqual(payload["error"]["code"], "invalid_env_file")
        self.assertIn("ERROR status Failed", completed.stderr)
        self.assertIn("Next action: Correct the command input and try again.", completed.stderr)

    def test_short_status_loads_local_env_without_shell_source(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            library = root / "library"
            data.mkdir()
            library.mkdir()
            database = data / "mediagent.sqlite3"
            database.touch()
            env_file = root / ".env"
            env_file.write_text(
                f"MEDIAGENT_DATA_DIR={data}\n"
                "MEDIAGENT_LIBRARY_DIR=${MEDIAGENT_DATA_DIR}/../library\n"
                "MEDIAGENT_DB_PATH=${MEDIAGENT_DATA_DIR}/mediagent.sqlite3\n",
                encoding="utf-8",
            )

            completed = self.run_cli(
                "status",
                "--json",
                env_updates={"MEDIAGENT_ENV_FILE": str(env_file)},
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["tool"], "core.env.check")

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

    def test_tools_run_invalid_json_emits_full_failure_payload(self) -> None:
        completed = self.run_cli(
            "tools",
            "run",
            "core.env.check",
            "--json",
            "--input",
            "-",
            input_text="{not-json",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("ERROR core.env.check Failed", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["tool"], "core.env.check")
        self.assertTrue(payload["run_id"])
        self.assertEqual(payload["status"], "failure")
        self.assertEqual(payload["data"], {})
        self.assertEqual(payload["artifacts"], [])
        self.assertEqual(payload["warnings"], [])
        self.assertIsNone(payload["rate_limit"])
        self.assertEqual(payload["error"]["code"], "invalid_input_file")
        self.assertEqual(payload["error"]["category"], "validation")
        self.assertEqual(payload["error"]["details"]["exception_type"], "ValueError")

    def test_tools_run_invalid_json_emits_summary_failure_payload(self) -> None:
        completed = self.run_cli(
            "tools",
            "run",
            "core.env.check",
            "--summary-json",
            "--input",
            "-",
            input_text="[]",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("ERROR core.env.check Failed", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["tool"], "core.env.check")
        self.assertTrue(payload["run_id"])
        self.assertEqual(payload["status"], "failure")
        self.assertEqual(payload["data"], {})
        self.assertEqual(payload["artifact_count"], 0)
        self.assertEqual(payload["warnings"], [])
        self.assertIsNone(payload["rate_limit"])
        self.assertEqual(payload["error"]["code"], "invalid_input_file")
        self.assertEqual(payload["error"]["category"], "validation")

    def test_tools_run_missing_input_file_is_structured(self) -> None:
        with TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.json"
            completed = self.run_cli(
                "tools",
                "run",
                "core.env.check",
                "--json",
                "--input",
                str(missing),
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("ERROR core.env.check Failed", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "failure")
        self.assertEqual(payload["error"]["code"], "invalid_input_file")
        self.assertEqual(payload["error"]["details"]["exception_type"], "FileNotFoundError")
        self.assertNotIn("Traceback", completed.stdout)
        self.assertNotIn(str(missing.parent), completed.stderr)

    def test_tools_run_permission_error_is_structured(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("permission denied")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = cli.run(
                [
                    "tools",
                    "run",
                    "core.env.check",
                    "--summary-json",
                    "--input",
                    "private.json",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("ERROR core.env.check Failed", stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "failure")
        self.assertEqual(payload["error"]["code"], "invalid_input_file")
        self.assertEqual(payload["error"]["details"]["exception_type"], "PermissionError")
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_compact_human_auth_status_does_not_confuse_tool_success_with_readiness(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            cli.print_compact_human_result(
                {
                    "tool": "jmcomic.auth.status",
                    "status": "success",
                    "data": {
                        "auth_status": "credentials_available_login_required",
                        "authenticated": False,
                        "reusable": False,
                    },
                    "warnings": [],
                    "error": None,
                }
            )

        self.assertEqual(stdout.getvalue(), "Credentials are configured, but login is required.\n")
        self.assertNotIn("status: success", stdout.getvalue())
        self.assertNotIn("{", stdout.getvalue())

    def test_compact_human_summary_is_concise_english(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            cli.print_compact_human_result(
                {
                    "tool": "link.media.sync",
                    "status": "success",
                    "data": {"summary": {"downloaded": 3, "skipped": 2, "failed": 0, "other": 99}},
                    "warnings": [],
                    "error": None,
                }
            )

        self.assertEqual(
            stdout.getvalue(),
            "The operation completed successfully.\nDownloaded: 3; Skipped: 2; Failed: 0.\n",
        )

    def test_compact_human_result_exposes_one_asset_id(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            cli.print_compact_human_result(
                {
                    "tool": "media.local.import",
                    "status": "success",
                    "data": {
                        "asset_ids": ["ast_example"],
                        "summary": {"imported": 1, "failed": 0},
                    },
                }
            )

        self.assertEqual(
            stdout.getvalue(),
            "The operation completed successfully.\nImported: 1; Failed: 0.\nAsset ID: ast_example\n",
        )

    def test_compact_human_lifecycle_results_are_actionable(self) -> None:
        cases = (
            ("library.asset.remove", {"result": "removed"}, "The Asset was moved to trash.\n"),
            ("library.asset.restore", {"result": "restored"}, "The Asset was restored.\n"),
            (
                "library.trash.purge",
                {"dry_run": True, "assets_ready": 3},
                "Trash purge preview found 3 Assets ready for permanent removal.\n",
            ),
            (
                "library.trash.purge",
                {"dry_run": False, "assets_purged": 2},
                "Permanently purged 2 Assets.\n",
            ),
        )
        for tool, data, expected in cases:
            with self.subTest(tool=tool):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    cli.print_compact_human_result(
                        {"status": "success", "tool": tool, "data": data}
                    )
                self.assertEqual(stdout.getvalue(), expected)

    def test_compact_human_auto_tag_result_is_actionable(self) -> None:
        cases = (
            ({"tagged": 2}, "Generated tags for 2 Assets.\n"),
            ({"unchanged": 1}, "Verified tags for 1 Asset.\n"),
            ({"deferred": 3}, "No automatic tagging jobs were ready.\n"),
        )
        for tagging, expected in cases:
            with self.subTest(tagging=tagging):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    cli.print_compact_human_result(
                        {
                            "status": "success",
                            "tool": "library.asset.tags.auto",
                            "data": {"tagging": tagging},
                        }
                    )
                self.assertEqual(stdout.getvalue(), expected)

    def test_tool_result_exit_mapping_remains_compatible(self) -> None:
        self.assertEqual(cli.tool_result_exit_code(ToolResult.success()), 0)
        self.assertEqual(
            cli.tool_result_exit_code(
                ToolResult.failure("bad_input", "Invalid input.", category=ErrorCategory.VALIDATION)
            ),
            2,
        )
        self.assertEqual(
            cli.tool_result_exit_code(
                ToolResult.failure("network_error", "Network failed.", category=ErrorCategory.NETWORK)
            ),
            1,
        )

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

    def test_asset_tag_and_search_cli_workflow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            library_root = Path(temp_dir) / "library"
            db_path = data_dir / "mediagent.sqlite3"
            source = library_root / "photo" / "evening.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"cli-search-content")
            db.initialize_database(db_path)
            db.upsert_media_item(
                db_path,
                {
                    "platform": "local",
                    "remote_id": "cli-search",
                    "media_type": "photo",
                    "status": "downloaded",
                    "metadata": {"title": "Evening Sky"},
                },
            )
            file_record = db.upsert_media_file(
                db_path,
                platform="local",
                remote_id="cli-search",
                remote_url=None,
                local_path=str(source),
                mime_type="image/jpeg",
                size_bytes=source.stat().st_size,
                checksum=library_content.sha256_checksum(source)[0],
                status="downloaded",
                library_relative_path="photo/evening.jpg",
                file_health="healthy",
            )
            adoption = library_content.adopt_media_file(db_path, file_id=file_record["id"])
            asset_id = adoption["asset_id"]
            env_updates = {
                "MEDIAGENT_DATA_DIR": str(data_dir),
                "MEDIAGENT_LIBRARY_DIR": str(library_root),
                "MEDIAGENT_DB_PATH": str(db_path),
            }

            tagged = self.run_cli("tag", asset_id, "favorite", env_updates=env_updates)
            searched = self.run_cli("search", "favorite", "Evening", "--json", env_updates=env_updates)
            untagged = self.run_cli("untag", asset_id, "FAVORITE", env_updates=env_updates)

            self.assertEqual(tagged.returncode, 0, tagged.stderr)
            self.assertEqual(tagged.stdout, "Added 1 tag to the Asset.\n")
            self.assertEqual(searched.returncode, 0, searched.stderr)
            search_payload = json.loads(searched.stdout)
            self.assertEqual(search_payload["data"]["count"], 1)
            self.assertEqual(search_payload["data"]["assets"][0]["asset_id"], asset_id)
            self.assertEqual(untagged.returncode, 0, untagged.stderr)
            self.assertEqual(untagged.stdout, "Removed 1 tag from the Asset.\n")

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
