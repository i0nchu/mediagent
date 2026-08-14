from __future__ import annotations

import asyncio
import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext, ToolResult
from mediagent.tools import comic_tools
from mediagent.tools.defaults import create_default_registry
from mediagent.platforms.nhentai.client import NhentaiApiError


class ImageHttpClient:
    def __init__(self, content: bytes, mime_type: str = "image/png") -> None:
        self.content = content
        self.mime_type = mime_type

    def get_limited(self, url, *, headers=None, timeout=30.0, max_bytes=1024 * 1024):
        return HttpResponse(200, {"Content-Type": self.mime_type, "Content-Length": str(len(self.content))}, self.content[:max_bytes], url)


class ComicToolTests(unittest.TestCase):
    def test_default_registry_exposes_simple_comic_surface(self) -> None:
        registry = create_default_registry()
        names = {spec.name for spec in registry.list()}
        self.assertTrue(
            {
                "comic.link.sync",
                "nhentai.auth.status",
                "nhentai.auth.refresh",
                "nhentai.favorites.collect",
                "nhentai.favorites.sync",
                "jmcomic.auth.status",
                "jmcomic.auth.login",
                "jmcomic.favorites.collect",
                "jmcomic.favorites.sync",
            }.issubset(names)
        )

    def test_direct_nhentai_link_is_exact_and_does_not_create_follow_state(self) -> None:
        item = _comic_item()
        context = ToolContext.from_env(
            dry_run=True,
            cwd=Path.cwd(),
            env={
                "MEDIAGENT_DATA_DIR": "/tmp/mediagent-comic-test",
                "MEDIAGENT_DB_PATH": "/tmp/mediagent-comic-test/state.sqlite3",
            },
        )
        with patch.object(comic_tools.nh_client, "resolve_exact", return_value=[item]), patch.object(
            comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))
        ):
            result = asyncio.run(
                comic_tools.comic_link_sync(context, {"url": "https://nhentai.net/g/123/"})
            )
        self.assertTrue(result.is_success)
        self.assertEqual(result.data["policy"], "exact")
        self.assertEqual(result.data["targets"][0]["target"], "gallery:123")

    def test_downloaded_pages_are_packaged_with_comicinfo(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            database = root / "state.sqlite3"
            content = _png_bytes()
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                    "MEDIAGENT_DB_PATH": str(database),
                },
                http_client=ImageHttpClient(content),
            )
            result = asyncio.run(
                comic_tools._sync_items(
                    context,
                    {"db_path": str(database), "library_root": str(library)},
                    [_comic_item()],
                )
            )
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(result.data["summary"]["cbz_packaged"], 1)
            cbz = Path(result.data["packages"][0]["target_path"])
            self.assertTrue(cbz.is_file())
            with zipfile.ZipFile(cbz) as archive:
                self.assertEqual(archive.namelist(), ["001.png", "ComicInfo.xml"])
                info = archive.read("ComicInfo.xml").decode()
            self.assertIn("<Publisher>Nhentai</Publisher>", info)
            self.assertIn("<PageCount>1</PageCount>", info)
            records = db.list_media_files(database, platform="nhentai", remote_id="gallery:123")
            self.assertEqual({record["file_key"] for record in records}, {"page:000001", "archive:cbz"})

    def test_overwrite_redownloads_terminal_comic_and_rebuilds_package(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            database = root / "state.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                    "MEDIAGENT_DB_PATH": str(database),
                },
                http_client=ImageHttpClient(_png_bytes()),
            )
            first = asyncio.run(comic_tools._sync_items(context, {}, [_comic_item()]))
            second = asyncio.run(
                comic_tools._sync_items(
                    context,
                    {"overwrite": True},
                    [_comic_item()],
                )
            )

        self.assertTrue(first.is_success, first.to_dict())
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(second.data["summary"]["queued"], 1)
        self.assertEqual(second.data["summary"]["downloaded"], 1)
        self.assertEqual(second.data["summary"]["cbz_packaged"], 1)

    def test_complete_snapshot_deactivates_removed_favorite_without_deleting_media(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.sqlite3"
            db.initialize_database(database)
            db.upsert_media_item(database, _comic_item())
            db.commit_collection_snapshot(
                database,
                provider="jmcomic",
                collection_key="favorites",
                targets=[{"target_type": "album", "target_id": "1"}],
            )
            db.commit_collection_snapshot(
                database,
                provider="jmcomic",
                collection_key="favorites",
                targets=[],
            )
            memberships = db.list_collection_memberships(
                database, provider="jmcomic", collection_key="favorites", active=False
            )
            with db.connect(database) as connection:
                count = connection.execute("SELECT COUNT(*) AS count FROM media_items").fetchone()["count"]
        self.assertEqual(memberships[0]["target_id"], "1")
        self.assertEqual(count, 1)

    def test_comic_page_rejects_non_image_response(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "MEDIAGENT_DB_PATH": str(root / "state.sqlite3"),
                },
                http_client=ImageHttpClient(b"video", "video/mp4"),
            )
            result = asyncio.run(comic_tools._sync_items(context, {}, [_comic_item()]))
        self.assertFalse(result.is_success)
        self.assertEqual(result.data["summary"]["failed"], 1)
        self.assertEqual(result.data["items"][0]["errors"][0]["code"], "download_validation_failed")

    def test_nhentai_favorites_refreshes_and_persists_expired_session_once(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(root / "state.sqlite3"),
                },
            )
            collection = {
                "complete": True,
                "targets": [{"provider_work_id": "gallery:123"}],
            }
            with patch.object(comic_tools.nh_auth, "load_session", return_value={"cookies": {}}), patch.object(
                comic_tools.nh_auth, "refresh_session", return_value={"cookies": {"refreshed": "yes"}}
            ) as refresh, patch.object(comic_tools.nh_auth, "save_session") as save, patch.object(
                comic_tools.nh_client,
                "collect_favorites",
                side_effect=[NhentaiApiError("nhentai_auth_required", "expired"), collection],
            ), patch.object(comic_tools.nh_client, "resolve_exact", return_value=[_comic_item()]), patch.object(
                comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))
            ):
                result = asyncio.run(comic_tools.nhentai_favorites_sync(context, {}))
        self.assertTrue(result.is_success)
        refresh.assert_called_once()
        save.assert_called_once()

    def test_nhentai_favorites_collect_reports_complete_snapshot_without_targets(self) -> None:
        context = ToolContext.from_env(dry_run=True, cwd=Path.cwd(), env={})
        collection = {
            "complete": True,
            "pages_fetched": 2,
            "expected_total": 1,
            "targets": [{"provider_work_id": "gallery:123", "title": "private favorite"}],
        }
        with patch.object(comic_tools.nh_auth, "load_session", return_value={"cookies": {}}), patch.object(
            comic_tools.nh_client, "collect_favorites", return_value=collection
        ):
            result = asyncio.run(comic_tools.nhentai_favorites_collect(context, {}))

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["favorites_seen"], 1)
        self.assertEqual(result.data["pages_fetched"], 2)
        self.assertNotIn("targets", result.data)

    def test_nhentai_refresh_403_reuses_session_when_favorites_still_work(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_path = root / "nhentai.json"
            comic_tools.nh_auth.save_session(
                {
                    "cookies": [
                        {
                            "name": "access_token",
                            "value": "opaque-test-token",
                            "domain": "nhentai.net",
                            "path": "/",
                            "secure": True,
                        }
                    ]
                },
                env={comic_tools.nh_auth.NHENTAI_SESSION_FILE_ENV: str(session_path)},
                cwd=root,
            )
            fake = _NhentaiRefreshFallbackHttpClient()
            context = ToolContext.from_env(
                cwd=root,
                env={comic_tools.nh_auth.NHENTAI_SESSION_FILE_ENV: str(session_path)},
                http_client=fake,
            )
            result = asyncio.run(comic_tools.nhentai_auth_refresh(context, {}))
        self.assertFalse(result.is_success, result.to_dict())
        self.assertEqual(result.error.code, "nhentai_refresh_rejected")
        self.assertEqual(result.data["operation"], "refresh")
        self.assertEqual(result.data["operation_status"], "failed")
        self.assertFalse(result.data["rotated"])
        self.assertTrue(result.data["current_auth_usable"])
        self.assertFalse(result.data["write_performed"])
        self.assertEqual(fake.calls, ["POST", "GET"])

    def test_jmcomic_auth_status_distinguishes_tool_success_from_login_state(self) -> None:
        context = ToolContext.from_env(
            cwd=Path.cwd(),
            env={
                "JMCOMIC_USERNAME": "configured-user",
                "JMCOMIC_PASSWORD": "configured-password",
                "JMCOMIC_SESSION_FILE": "/tmp/mediagent-missing-jmcomic-session.json",
            },
        )
        result = asyncio.run(comic_tools.jmcomic_auth_status(context, {}))
        self.assertTrue(result.is_success)
        self.assertEqual(result.data["auth_status"], "credentials_available_login_required")
        self.assertFalse(result.data["authenticated"])
        self.assertFalse(result.data["session_present"])
        self.assertFalse(result.data["reusable"])

    def test_jmcomic_favorites_collect_reports_follow_count_without_targets(self) -> None:
        context = ToolContext.from_env(dry_run=True, cwd=Path.cwd(), env={})
        collection = SimpleNamespace(
            pages_fetched=3,
            total=2,
            items=(SimpleNamespace(album_id="1"), SimpleNamespace(album_id="2")),
        )
        client = SimpleNamespace(collect_favorites=lambda: collection)
        with patch.object(comic_tools, "_jm_client", return_value=client):
            result = asyncio.run(comic_tools.jmcomic_favorites_collect(context, {}))

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["favorites_seen"], 2)
        self.assertEqual(result.data["following"], 2)
        self.assertNotIn("targets", result.data)

    def test_changed_tracked_manifest_rebuilds_existing_cbz(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            database = root / "state.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                    "MEDIAGENT_DB_PATH": str(database),
                },
                http_client=ImageHttpClient(_png_bytes()),
            )
            first = asyncio.run(comic_tools._sync_items(context, {}, [_comic_item()]))
            changed = _comic_item()
            changed["metadata"]["page_count"] = 2
            changed["metadata"]["files"].append(
                {
                    "url": "https://1.1.1.1/page-2.png",
                    "kind": "image",
                    "page": 1,
                    "page_number": 2,
                    "mime_type": "image/png",
                    "extension": ".png",
                    "storage_category": "comic-pages",
                }
            )
            second = asyncio.run(comic_tools._sync_items(context, {}, [changed]))
            cbz = Path(second.data["packages"][0]["target_path"])
            with zipfile.ZipFile(cbz) as archive:
                names = archive.namelist()
        self.assertTrue(first.is_success)
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(names, ["001.png", "002.png", "ComicInfo.xml"])

    def test_removed_manifest_page_rebuilds_cbz_without_deleting_old_source(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "MEDIAGENT_DB_PATH": str(root / "state.sqlite3"),
                },
                http_client=ImageHttpClient(_png_bytes()),
            )
            two_pages = _comic_item()
            two_pages["metadata"]["page_count"] = 2
            two_pages["metadata"]["files"].append(
                {
                    "url": "https://1.1.1.1/page-2.png",
                    "kind": "image",
                    "page": 1,
                    "page_number": 2,
                    "mime_type": "image/png",
                    "extension": ".png",
                    "storage_category": "comic-pages",
                }
            )
            first = asyncio.run(comic_tools._sync_items(context, {}, [two_pages]))
            old_sources = [
                Path(record["local_path"])
                for record in db.list_media_files(
                    root / "state.sqlite3", platform="nhentai", remote_id="gallery:123"
                )
                if record["mime_type"] == "image/png"
            ]
            second = asyncio.run(comic_tools._sync_items(context, {}, [_comic_item()]))
            cbz = Path(second.data["packages"][0]["target_path"])
            with zipfile.ZipFile(cbz) as archive:
                names = archive.namelist()
            old_sources_exist = all(path.exists() for path in old_sources)
        self.assertTrue(first.is_success)
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(names, ["001.png", "ComicInfo.xml"])
        self.assertTrue(old_sources_exist)

    def test_large_collection_dry_run_chunks_identity_queries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            db.initialize_database(database)
            context = ToolContext.from_env(
                dry_run=True,
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "MEDIAGENT_DB_PATH": str(database),
                },
            )
            items = []
            for index in range(1_100):
                item = json.loads(json.dumps(_comic_item()))
                item["remote_id"] = f"gallery:{index}"
                item["metadata"]["comic"]["provider_work_id"] = f"gallery:{index}"
                items.append(item)

            result = asyncio.run(comic_tools._sync_items(context, {}, items))

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["summary"]["resolved_items"], 1_100)

    def test_favorite_sync_continues_after_one_target_resolution_failure(self) -> None:
        context = ToolContext.from_env(
            dry_run=True,
            cwd=Path.cwd(),
            env={
                "MEDIAGENT_DATA_DIR": "/tmp/mediagent-comic-test",
                "MEDIAGENT_LIBRARY_DIR": "/tmp/mediagent-comic-test/library",
                "MEDIAGENT_DB_PATH": "/tmp/mediagent-comic-test/state.sqlite3",
            },
        )
        targets = [
            {"target_type": "album", "target_id": "bad"},
            {"target_type": "album", "target_id": "good"},
        ]

        def resolve_target(target):
            if target["target_id"] == "bad":
                raise ValueError("removed target")
            return [_comic_item()]

        with patch.object(
            comic_tools,
            "_sync_items",
            new=AsyncMock(
                return_value=ToolResult.success(
                    {"summary": {"resolved_items": 1, "queued": 1, "planned_files": 1}}
                )
            ),
        ) as sync_items:
            result = asyncio.run(
                comic_tools._sync_favorite_targets(context, {}, targets, resolve_target)
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "comic_favorites_sync_partial")
        self.assertEqual(result.data["summary"]["targets_processed"], 1)
        self.assertEqual(result.data["summary"]["targets_failed"], 1)
        sync_items.assert_awaited_once()


def _comic_item() -> dict:
    return {
        "platform": "nhentai",
        "remote_id": "gallery:123",
        "source_url": "https://nhentai.net/g/123/",
        "media_type": "photo",
        "status": "discovered",
        "source_availability": "available",
        "source_timestamp": "2026-01-02T00:00:00+00:00",
        "metadata": {
            "title": "Example",
            "work_type": "comic",
            "storage_category": "comic-pages",
            "page_count": 1,
            "comic": {
                "provider": "nhentai",
                "provider_work_id": "gallery:123",
                "title": "Example",
                "is_one_shot": True,
                "total_count": 1,
            },
            "files": [
                {
                    "url": "https://1.1.1.1/page.png",
                    "kind": "image",
                    "page": 0,
                    "page_number": 1,
                    "mime_type": "image/png",
                    "extension": ".png",
                    "storage_category": "comic-pages",
                }
            ],
        },
    }


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()


class _NhentaiRefreshFallbackHttpClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def post_form(self, url, data, *, headers=None, timeout=30.0):
        self.calls.append("POST")
        return HttpResponse(403, {}, b"denied", url)

    def get_json(self, url, *, headers=None, timeout=30.0):
        self.calls.append("GET")
        return HttpResponse(200, {}, b'{"result": [], "num_pages": 1}', url)


if __name__ == "__main__":
    unittest.main()
