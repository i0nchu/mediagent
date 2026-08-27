import asyncio
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext
from mediagent.tools.defaults import create_default_registry


class FakeHttpClient:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0
        self.headers: dict[str, str] | None = None

    def get(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.calls += 1
        self.headers = headers
        return self.response


class BottomToolTests(unittest.TestCase):
    def test_db_init_is_idempotent(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
            )

            first = asyncio.run(registry.run("core.db.init", {}, context))
            second = asyncio.run(registry.run("core.db.init", {}, context))

            self.assertTrue(first.is_success)
            self.assertTrue(second.is_success)
            self.assertTrue(db_path.exists())
            self.assertEqual(second.data["schema_version"], "10")

    def test_db_connections_wait_for_short_lived_writer_contention(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            with db.connect(db_path) as connection:
                busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(busy_timeout, db.SQLITE_BUSY_TIMEOUT_MILLISECONDS)

    def test_run_record_writes_summary_without_secrets(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
            )

            result = asyncio.run(
                registry.run(
                    "core.run.record",
                    {
                        "name": "sample",
                        "status": "success",
                        "summary": {"count": 1, "api_token": "secret"},
                    },
                    context,
                )
            )

            self.assertTrue(result.is_success)
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT summary_json FROM runs WHERE id = ?",
                    (result.data["run_id"],),
                ).fetchone()
            summary = json.loads(row[0])
            self.assertEqual(summary["api_token"], "<redacted>")

    def test_db_init_dry_run_does_not_write(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(registry.run("core.db.init", {}, context))

            self.assertTrue(result.is_success)
            self.assertFalse(db_path.exists())
            self.assertTrue(result.data["would_initialize"])

    def test_path_prepare_rejects_path_traversal(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "core.path.prepare",
                    {"path": "../outside.txt", "base_dir": str(data_dir)},
                    context,
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "unsafe_path")
            self.assertEqual(result.error.category.value, "filesystem")

    def test_sync_cursor_helpers(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
            )

            set_result = asyncio.run(
                registry.run(
                    "core.sync_cursor.set",
                    {
                        "platform": "x",
                        "cursor_name": "bookmarks",
                        "cursor_value": "next-token",
                    },
                    context,
                )
            )
            get_result = asyncio.run(
                registry.run(
                    "core.sync_cursor.get",
                    {"platform": "x", "cursor_name": "bookmarks"},
                    context,
                )
            )

            self.assertTrue(set_result.is_success)
            self.assertTrue(get_result.is_success)
            self.assertEqual(get_result.data["cursor"]["cursor_value"], "next-token")

    def test_media_upsert_and_filter_new(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
            )
            item = {
                "platform": "pixiv",
                "remote_id": "123",
                "media_type": "photo",
                "source_url": "https://example.invalid/123",
                "metadata": {"token": "secret", "title": "sample"},
            }

            upsert = asyncio.run(
                registry.run("media.item.upsert", {"items": [item]}, context)
            )
            filtered = asyncio.run(
                registry.run("media.item.filter_new", {"items": [item]}, context)
            )

            self.assertTrue(upsert.is_success)
            self.assertEqual(upsert.data["items"][0]["is_new"], True)
            self.assertTrue(filtered.is_success)
            self.assertEqual(filtered.data["summary"]["known"], 1)
            self.assertEqual(filtered.data["items"][0]["metadata"]["token"], "<redacted>")

            file_result = asyncio.run(
                registry.run(
                    "media.file.upsert",
                    {
                        "platform": "pixiv",
                        "remote_id": "123",
                        "remote_url": "https://example.invalid/123.jpg",
                        "local_path": str(Path(temp_dir) / "123.jpg"),
                        "mime_type": "image/jpeg",
                        "size_bytes": 4,
                        "checksum": "sha256:abcd",
                        "status": "downloaded",
                    },
                    context,
                )
            )
            self.assertTrue(file_result.is_success)
            self.assertEqual(file_result.data["file"]["status"], "downloaded")

    def test_media_item_set_status_updates_filtering(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
            )
            item = {
                "platform": "pixiv",
                "remote_id": "status-123",
                "media_type": "photo",
            }

            asyncio.run(registry.run("media.item.upsert", {"items": [item]}, context))
            update = asyncio.run(
                registry.run(
                    "media.item.set_status",
                    {
                        "platform": "pixiv",
                        "remote_id": "status-123",
                        "status": "downloaded",
                    },
                    context,
                )
            )
            filtered = asyncio.run(
                registry.run("media.item.filter_new", {"items": [item]}, context)
            )

        self.assertTrue(update.is_success)
        self.assertEqual(update.data["item"]["status"], "downloaded")
        self.assertTrue(filtered.is_success)
        self.assertEqual(filtered.data["summary"]["downloaded"], 1)
        self.assertEqual(filtered.data["items"], [])

    def test_media_item_set_status_migrates_old_media_items_schema(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3')")
                connection.execute(
                    """
                    CREATE TABLE media_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        remote_id TEXT NOT NULL,
                        source_url TEXT,
                        author_id TEXT,
                        author_name TEXT,
                        media_type TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'discovered',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform, remote_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO media_items (
                        platform, remote_id, media_type, status, metadata_json,
                        created_at, updated_at
                    )
                    VALUES ('pixiv', 'old-1', 'photo', 'discovered', '{}', 'old', 'old')
                    """
                )
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
            )

            result = asyncio.run(
                registry.run(
                    "media.item.set_status",
                    {
                        "platform": "pixiv",
                        "remote_id": "old-1",
                        "status": "downloaded",
                    },
                    context,
                )
            )
            with sqlite3.connect(db_path) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(media_items)").fetchall()}
                schema_version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                downloaded_at = connection.execute(
                    "SELECT downloaded_at FROM media_items WHERE platform = 'pixiv' AND remote_id = 'old-1'"
                ).fetchone()[0]

        self.assertTrue(result.is_success)
        self.assertIn("downloaded_at", columns)
        self.assertEqual(schema_version, "10")
        self.assertIsNotNone(downloaded_at)

    def test_media_file_upsert_is_idempotent_with_null_remote_url(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
            )
            item = {
                "platform": "pixiv",
                "remote_id": "456",
                "media_type": "photo",
            }
            asyncio.run(registry.run("media.item.upsert", {"items": [item]}, context))

            first = asyncio.run(
                registry.run(
                    "media.file.upsert",
                    {
                        "platform": "pixiv",
                        "remote_id": "456",
                        "local_path": str(Path(temp_dir) / "456.jpg"),
                        "size_bytes": 4,
                        "status": "discovered",
                    },
                    context,
                )
            )
            second = asyncio.run(
                registry.run(
                    "media.file.upsert",
                    {
                        "platform": "pixiv",
                        "remote_id": "456",
                        "local_path": str(Path(temp_dir) / "456.jpg"),
                        "size_bytes": 8,
                        "status": "downloaded",
                    },
                    context,
                )
            )

            with sqlite3.connect(db_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]

        self.assertTrue(first.is_success)
        self.assertTrue(second.is_success)
        self.assertEqual(first.data["file"]["id"], second.data["file"]["id"])
        self.assertEqual(second.data["file"]["size_bytes"], 8)
        self.assertEqual(count, 1)

    def test_library_file_verify_marks_known_file_health(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            library_dir = Path(temp_dir) / "library"
            db_path = data_dir / "mediagent.sqlite3"
            valid_path = library_dir / "photo/2026/01/valid.jpg"
            corrupt_path = library_dir / "photo/2026/01/corrupt.jpg"
            valid_path.parent.mkdir(parents=True)
            valid_path.write_bytes(b"valid")
            corrupt_path.write_bytes(b"bad")
            valid_checksum = "sha256:" + hashlib.sha256(b"valid").hexdigest()
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_LIBRARY_DIR": str(library_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                },
                cwd=Path(temp_dir),
            )

            for remote_id, relative_path, size, checksum in (
                ("valid", "photo/2026/01/valid.jpg", 5, valid_checksum),
                ("missing", "photo/2026/01/missing.jpg", 7, "sha256:missing"),
                ("corrupt", "photo/2026/01/corrupt.jpg", 7, "sha256:corrupt"),
            ):
                asyncio.run(
                    registry.run(
                        "media.item.upsert",
                        {"items": [{"platform": "pixiv", "remote_id": remote_id, "media_type": "photo"}]},
                        context,
                    )
                )
                asyncio.run(
                    registry.run(
                        "media.file.upsert",
                        {
                            "platform": "pixiv",
                            "remote_id": remote_id,
                            "local_path": str(library_dir / relative_path),
                            "library_relative_path": relative_path,
                            "size_bytes": size,
                            "checksum": checksum,
                            "status": "downloaded",
                        },
                        context,
                    )
                )

            result = asyncio.run(registry.run("library.file.verify", {}, context))
            rows = {
                row["remote_id"]: row["file_health"]
                for row in db.list_media_files(db_path, platform="pixiv", status="downloaded")
            }

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["valid"], 1)
        self.assertEqual(result.data["summary"]["missing"], 1)
        self.assertEqual(result.data["summary"]["corrupt"], 1)
        self.assertEqual(rows["valid"], "valid")
        self.assertEqual(rows["missing"], "missing")
        self.assertEqual(rows["corrupt"], "corrupt")

    def test_library_file_verify_requires_selector_for_custom_root(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            library_dir = data_dir / "library"
            custom_root = data_dir / "live-test" / "library"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_LIBRARY_DIR": str(library_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                },
                cwd=Path(temp_dir),
            )
            db.initialize_database(db_path)

            result = asyncio.run(
                registry.run(
                    "library.file.verify",
                    {"library_root": str(custom_root)},
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "custom_library_root_requires_selector")

    def test_download_http_uses_fake_client(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            response = HttpResponse(
                status_code=200,
                headers={"content-type": "image/jpeg", "content-length": "4"},
                content=b"test",
            )
            fake_client = FakeHttpClient(response)
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                http_client=fake_client,
            )

            result = asyncio.run(
                registry.run(
                    "download.http",
                    {
                        "url": "https://example.invalid/file.jpg",
                        "target_dir": str(data_dir),
                        "filename": "file.jpg",
                        "expected_mime_prefix": "image/",
                    },
                    context,
                )
            )

            self.assertTrue(result.is_success)
            self.assertEqual(fake_client.calls, 1)
            self.assertEqual((data_dir / "file.jpg").read_bytes(), b"test")
            self.assertFalse((data_dir / "file.jpg.partial").exists())
            self.assertEqual(result.data["partial_path"], str(data_dir / "file.jpg.partial"))
            self.assertTrue(result.data["finalized"])
            self.assertTrue(result.data["checksum"].startswith("sha256:"))

    def test_storage_path_plan_uses_library_dir_and_source_date(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            library_dir = Path(temp_dir) / "library"
            context = ToolContext.from_env(
                env={"MEDIAGENT_LIBRARY_DIR": str(library_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "storage.path.plan",
                    {
                        "item": {
                            "platform": "pixiv",
                            "remote_id": "143734851",
                            "media_type": "photo",
                            "metadata": {"create_date": "2026-07-22T01:02:03+09:00"},
                        },
                        "file": {
                            "url": "https://i.pximg.net/img-original/img/2026/07/22/143734851_p0.jpg",
                            "page": 0,
                        },
                        "create_dirs": True,
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["layout"], "scanner-friendly-v2")
        self.assertTrue(result.data["platform_layer_included"])
        self.assertEqual(
            result.data["relative_path"],
            "pixiv/photo/2026/07/20260722__pixiv__143734851__p0.jpg",
        )
        self.assertEqual(result.data["date_source"], "source")
        self.assertTrue(result.data["would_create_dirs"])
        self.assertFalse((library_dir / "pixiv").exists())

    def test_storage_path_plan_falls_back_to_data_dir_library(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "storage.path.plan",
                    {
                        "item": {
                            "platform": "x",
                            "remote_id": "post-1",
                            "media_type": "video",
                            "metadata": {"create_date": "2026-07-22T00:00:00+00:00"},
                        },
                        "file": {"url": "https://video.example.invalid/file.mp4", "page": 0},
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["library_root"], str(data_dir / "library"))
        self.assertEqual(
            result.data["relative_path"],
            "x/video/2026/07/20260722__x__post-1__v0.mp4",
        )

    def test_storage_path_plan_uses_file_media_type_for_mixed_post_files(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "storage.path.plan",
                    {
                        "item": {
                            "platform": "instagram",
                            "remote_id": "carousel-1",
                            "media_type": "photo",
                            "metadata": {"create_date": "2026-07-22T00:00:00+00:00"},
                        },
                        "file": {
                            "url": "https://www.instagram.com/p/carousel-1/?mediagent_resource=4",
                            "media_type": "video",
                            "extension": ".mp4",
                            "part": "v0",
                        },
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(
            result.data["relative_path"],
            "instagram/video/2026/07/20260722__instagram__carousel-1__v0.mp4",
        )

    def test_storage_path_plan_uses_platform_specific_library_dir(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            pixiv_dir = Path(temp_dir) / "pixiv-library"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_PIXIV_LIBRARY_DIR": str(pixiv_dir),
                },
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "storage.path.plan",
                    {
                        "item": {
                            "platform": "pixiv",
                            "remote_id": "1001",
                            "media_type": "photo",
                            "metadata": {"create_date": "2026-01-01T00:00:00+00:00"},
                        },
                        "file": {"url": "https://example.invalid/1001_p0.jpg", "page": 0},
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["library_root"], str(pixiv_dir))
        self.assertFalse(result.data["platform_layer_included"])
        self.assertEqual(
            result.data["relative_path"],
            "photo/2026/01/20260101__pixiv__1001__p0.jpg",
        )
        self.assertTrue(result.data["final_path"].startswith(str(pixiv_dir)))

    def test_storage_path_plan_rejects_unsupported_media_type(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "storage.path.plan",
                    {
                        "item": {"platform": "pixiv", "remote_id": "1", "media_type": "document"},
                        "file": {"url": "https://example.invalid/file.bin"},
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_path")

    def test_download_http_passes_custom_headers(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            response = HttpResponse(
                status_code=200,
                headers={"content-type": "image/png", "content-length": "4"},
                content=b"test",
            )
            fake_client = FakeHttpClient(response)
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                http_client=fake_client,
            )

            result = asyncio.run(
                registry.run(
                    "download.http",
                    {
                        "url": "https://i.pximg.net/img-original/example.png",
                        "target_dir": str(data_dir),
                        "filename": "example.png",
                        "expected_mime_prefix": "image/",
                        "headers": {"Referer": "https://www.pixiv.net/"},
                    },
                    context,
                )
            )

            self.assertTrue(result.is_success)
            self.assertEqual(fake_client.headers, {"Referer": "https://www.pixiv.net/"})

    def test_download_http_rate_limit_metadata(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            response = HttpResponse(
                status_code=429,
                headers={
                    "x-rate-limit-limit": "15",
                    "x-rate-limit-remaining": "0",
                    "x-rate-limit-reset": "1234567890",
                },
                content=b"{}",
            )
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                http_client=FakeHttpClient(response),
            )

            result = asyncio.run(
                registry.run(
                    "download.http",
                    {
                        "url": "https://example.invalid/file.jpg",
                        "target_dir": str(data_dir),
                        "filename": "file.jpg",
                    },
                    context,
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.category.value, "rate_limit")
            self.assertEqual(result.rate_limit["remaining"], 0)

    def test_download_http_dry_run_does_not_call_network_or_write(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            response = HttpResponse(status_code=200, headers={}, content=b"test")
            fake_client = FakeHttpClient(response)
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                http_client=fake_client,
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "download.http",
                    {
                        "url": "https://example.invalid/file.jpg",
                        "target_dir": str(data_dir),
                        "filename": "file.jpg",
                    },
                    context,
                )
            )

            self.assertTrue(result.is_success)
            self.assertTrue(result.data["would_download"])
            self.assertEqual(fake_client.calls, 0)
            self.assertFalse((data_dir / "file.jpg").exists())

    def test_metadata_write_redacts_secrets(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
            )

            result = asyncio.run(
                registry.run(
                    "metadata.write",
                    {
                        "target_dir": str(data_dir),
                        "filename": "metadata.json",
                        "metadata": {"title": "sample", "refresh_token": "secret"},
                    },
                    context,
                )
            )

            self.assertTrue(result.is_success)
            written = json.loads((data_dir / "metadata.json").read_text())
            self.assertEqual(written["refresh_token"], "<redacted>")

    def test_metadata_write_dry_run_does_not_write(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "metadata.write",
                    {
                        "target_dir": str(data_dir),
                        "filename": "metadata.json",
                        "metadata": {"title": "sample"},
                    },
                    context,
                )
            )

            self.assertTrue(result.is_success)
            self.assertTrue(result.data["would_write"])
            self.assertFalse((data_dir / "metadata.json").exists())
