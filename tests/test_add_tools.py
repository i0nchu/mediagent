import asyncio
import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from mediagent.core.tooling import ErrorCategory, ToolContext, ToolResult
from mediagent.core import db
from mediagent.tools import add_tools
from mediagent.tools.defaults import create_default_registry


class AddToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ToolContext(
            cwd=Path("/tmp"),
            env={},
            dry_run=False,
            run_id="test-run",
        )

    def test_registry_exposes_unified_add(self) -> None:
        spec = create_default_registry().get("media.add").spec

        self.assertEqual(spec.name, "media.add")
        self.assertTrue(spec.dry_run_supported)

    def test_generic_url_delegates_with_smart_repair_defaults(self) -> None:
        delegated = ToolResult.success({"asset_ids": ["asset_one"], "summary": {"downloaded": 1}})
        with (
            patch("mediagent.tools.add_tools.comic_tools.comic_link_provider", return_value=None),
            patch("mediagent.tools.add_tools.link_tools.media_sync", new=AsyncMock(return_value=delegated)) as sync,
        ):
            result = self._run({"input": "https://example.com/file.jpg"})

        self.assertIs(result, delegated)
        sync.assert_awaited_once_with(
            self.context,
            {
                "url": "https://example.com/file.jpg",
                "overwrite": False,
                "retry_failed": True,
                "repair_missing_files": True,
                "write_sidecar_metadata": False,
            },
        )
        self.assertEqual(
            result.data["add"],
            {"input_kind": "url", "delegated_tool": "link.media.sync"},
        )
        self.assertEqual(result.data["asset_ids"], ["asset_one"])

    def test_comic_url_delegates_without_generic_sidecar_option(self) -> None:
        delegated = ToolResult.success({"asset_ids": []})
        with (
            patch("mediagent.tools.add_tools.comic_tools.comic_link_provider", return_value="nhentai"),
            patch("mediagent.tools.add_tools.comic_tools.comic_link_sync", new=AsyncMock(return_value=delegated)) as sync,
        ):
            result = self._run(
                {
                    "input": "https://nhentai.net/g/123/",
                    "overwrite": True,
                    "repair": False,
                    "db_path": "/tmp/test.sqlite3",
                    "library_root": "/tmp/library",
                }
            )

        sync.assert_awaited_once_with(
            self.context,
            {
                "url": "https://nhentai.net/g/123/",
                "overwrite": True,
                "retry_failed": False,
                "repair_missing_files": False,
                "db_path": "/tmp/test.sqlite3",
                "library_root": "/tmp/library",
            },
        )
        self.assertEqual(result.data["add"]["input_kind"], "comic_url")

    def test_local_input_delegates_copy_only_and_preserves_failure(self) -> None:
        delegated = ToolResult.failure(
            "local_input_not_found",
            "The local input path does not exist.",
            data={"asset_ids": []},
            category=ErrorCategory.FILESYSTEM,
        )
        with patch(
            "mediagent.tools.add_tools.local_import_tools.import_local_media",
            return_value=delegated,
        ) as import_media:
            result = self._run(
                {
                    "input": "./incoming",
                    "overwrite": True,
                    "repair": False,
                    "library_root": "/tmp/library",
                }
            )

        import_media.assert_called_once_with(
            self.context,
            {"path": "./incoming", "library_root": "/tmp/library"},
        )
        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "local_input_not_found")
        self.assertEqual(
            result.data["add"],
            {"input_kind": "local", "delegated_tool": "media.local.import"},
        )

    def test_blank_input_fails_before_delegation(self) -> None:
        with patch("mediagent.tools.add_tools.local_import_tools.import_local_media", new=Mock()) as import_media:
            result = self._run({"input": "  "})

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "invalid_add_input")
        import_media.assert_not_called()

    def test_local_input_runs_real_copy_only_intake(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            library_dir = root / "library"
            db_path = data_dir / "mediagent.sqlite3"
            source = root / "one.png"
            source.write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
            )
            db.initialize_database(db_path)
            context = ToolContext(
                cwd=root,
                env={"MEDIAGENT_AUTO_TAG": "false"},
                dry_run=False,
                run_id="real-add",
                data_dir=data_dir,
                library_dir=library_dir,
                db_path=db_path,
            )

            result = asyncio.run(add_tools.media_add(context, {"input": str(source)}))

            self.assertTrue(result.is_success)
            self.assertEqual(result.data["add"]["delegated_tool"], "media.local.import")
            self.assertEqual(result.data["summary"]["imported"], 1)
            self.assertEqual(len(result.data["asset_ids"]), 1)
            self.assertEqual(len(result.artifacts), 1)
            self.assertTrue(Path(result.artifacts[0]["path"]).is_file())

    def _run(self, input_data: dict[str, object]) -> ToolResult:
        return asyncio.run(add_tools.media_add(self.context, input_data))


if __name__ == "__main__":
    unittest.main()
