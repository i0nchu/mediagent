from __future__ import annotations

import asyncio
import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext, ToolResult
from mediagent.tools import comic_tools
from mediagent.tools.defaults import create_default_registry
from mediagent.platforms.nhentai.client import NhentaiApiError
from mediagent.platforms.jmcomic.auth import JMComicSession
from mediagent.platforms.jmcomic.client import JMComicClientError


class ImageHttpClient:
    def __init__(self, content: bytes, mime_type: str = "image/png") -> None:
        self.content = content
        self.mime_type = mime_type

    def get_limited(self, url, *, headers=None, timeout=30.0, max_bytes=1024 * 1024):
        return HttpResponse(200, {"Content-Type": self.mime_type, "Content-Length": str(len(self.content))}, self.content[:max_bytes], url)


class MappingImageHttpClient:
    def __init__(self, content_by_url: dict[str, bytes], mime_type: str = "image/webp") -> None:
        self.content_by_url = content_by_url
        self.mime_type = mime_type
        self.calls: list[str] = []

    def get_limited(self, url, *, headers=None, timeout=30.0, max_bytes=1024 * 1024):
        self.calls.append(url)
        content = self.content_by_url[url]
        return HttpResponse(
            200,
            {"Content-Type": self.mime_type, "Content-Length": str(len(content))},
            content[:max_bytes],
            url,
        )


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
                "jmcomic.favorites.folders.register",
                "jmcomic.favorites.folders.list",
                "jmcomic.favorites.folders.collect",
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

    def test_jmcomic_spacer_is_ignored_and_excluded_from_cbz_and_repair(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            database = root / "state.sqlite3"
            item = _jmcomic_item_with_spacer()
            urls = [file_info["url"] for file_info in item["metadata"]["files"]]
            client = MappingImageHttpClient(
                {
                    urls[0]: _webp_bytes(8, 16, "red"),
                    urls[1]: _webp_bytes(720, 3, "white"),
                }
            )
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                    "MEDIAGENT_DB_PATH": str(database),
                },
                http_client=client,
            )

            first = asyncio.run(comic_tools._sync_items(context, {}, [item]))
            records = db.list_media_files(database, platform="jmcomic", remote_id="photo:1234567")
            second = asyncio.run(comic_tools._sync_items(context, {}, [item]))

            cbz = Path(first.data["packages"][0]["target_path"])
            with zipfile.ZipFile(cbz) as archive:
                archive_names = archive.namelist()
                comic_info = archive.read("ComicInfo.xml").decode()
            page_files = list(library.rglob("*.webp"))

        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(first.data["items"][0]["status"], "downloaded")
        self.assertEqual(first.data["items"][0]["files_downloaded"], 1)
        self.assertEqual(first.data["items"][0]["files_skipped"], 1)
        self.assertEqual(first.data["summary"]["files_skipped"], 1)
        self.assertEqual(first.data["summary"]["cbz_packaged"], 1)
        self.assertEqual(archive_names, ["001.webp", "ComicInfo.xml"])
        self.assertIn("<PageCount>1</PageCount>", comic_info)
        self.assertEqual(len(page_files), 1)
        spacer = next(record for record in records if record["file_key"] == "page:000002")
        self.assertEqual(spacer["status"], "skipped")
        self.assertEqual(spacer["file_health"], "ignored_spacer")
        self.assertIsNone(spacer["local_path"])
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(second.data["summary"]["queued"], 0)
        self.assertEqual(second.data["summary"]["skipped_healthy"], 1)
        self.assertEqual(len(client.calls), 2)

    def test_malformed_tiny_jmcomic_response_still_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _jmcomic_item_with_spacer()
            item["metadata"]["files"] = [item["metadata"]["files"][1]]
            item["metadata"]["page_count"] = 1
            url = item["metadata"]["files"][0]["url"]
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "MEDIAGENT_DB_PATH": str(root / "state.sqlite3"),
                },
                http_client=MappingImageHttpClient({url: b"not-an-image"}),
            )

            result = asyncio.run(comic_tools._sync_items(context, {}, [item]))

        self.assertFalse(result.is_success)
        self.assertEqual(result.data["items"][0]["status"], "failed")
        self.assertEqual(result.data["items"][0]["errors"][0]["code"], "download_transform_failed")

    def test_all_spacer_jmcomic_chapter_is_terminal_without_empty_cbz(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _jmcomic_item_with_spacer()
            item["metadata"]["files"] = [item["metadata"]["files"][1]]
            item["metadata"]["page_count"] = 1
            url = item["metadata"]["files"][0]["url"]
            client = MappingImageHttpClient({url: _webp_bytes(720, 3, "white")})
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "MEDIAGENT_DB_PATH": str(root / "state.sqlite3"),
                },
                http_client=client,
            )

            first = asyncio.run(comic_tools._sync_items(context, {}, [item]))
            second = asyncio.run(comic_tools._sync_items(context, {}, [item]))

        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(first.data["items"][0]["status"], "skipped")
        self.assertEqual(first.data["packages"][0]["status"], "skipped")
        self.assertEqual(first.data["summary"]["cbz_failed_or_incomplete"], 0)
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(second.data["summary"]["queued"], 0)
        self.assertEqual(len(client.calls), 1)

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
            items=(_jm_favorite("1"), _jm_favorite("2")),
        )
        client = SimpleNamespace(collect_favorites=lambda **_: collection)
        with patch.object(comic_tools, "_jm_client", return_value=client):
            result = asyncio.run(comic_tools.jmcomic_favorites_collect(context, {}))

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["favorites_seen"], 2)
        self.assertEqual(result.data["following"], 2)
        self.assertNotIn("targets", result.data)

    def test_jmcomic_folder_alias_registers_and_lists_without_network(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(database),
                    "JMCOMIC_USERNAME": "configured-user",
                },
            )
            registered = asyncio.run(
                comic_tools.jmcomic_favorite_folder_register(
                    context,
                    {
                        "name": "稍後閱讀",
                        "url": "https://18comic.vip/user/example/favorite/albums?folder=1234567",
                    },
                )
            )
            listed = asyncio.run(comic_tools.jmcomic_favorite_folders_list(context, {}))

        self.assertTrue(registered.is_success, registered.to_dict())
        self.assertTrue(listed.is_success, listed.to_dict())
        self.assertEqual(registered.data["folder"]["folder_id"], "1234567")
        self.assertEqual(
            [(folder["name"], folder["folder_id"]) for folder in listed.data["folders"]],
            [("all", "0"), ("稍後閱讀", "1234567")],
        )

    def test_jmcomic_folder_selection_accepts_env_ids_and_urls_and_deduplicates(self) -> None:
        context = ToolContext.from_env(
            dry_run=True,
            cwd=Path.cwd(),
            env={
                "MEDIAGENT_JMCOMIC_FAVORITE_FOLDERS": json.dumps(
                    [
                        "1234567",
                        "https://18comic.vip/user/example/favorite/albums?folder=1234567",
                    ]
                )
            },
        )
        collection = SimpleNamespace(items=(_jm_favorite("1"),), pages_fetched=1)
        client = SimpleNamespace(collect_favorites=MagicMock(return_value=collection))
        with patch.object(comic_tools, "_jm_client", return_value=client):
            result = asyncio.run(comic_tools.jmcomic_favorites_collect(context, {}))

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["favorites_seen"], 1)
        self.assertEqual([folder["folder_id"] for folder in result.data["folders"]], ["1234567"])
        client.collect_favorites.assert_called_once_with(folder_id="1234567")

    def test_jmcomic_folder_name_is_discovered_from_remote_folder_list(self) -> None:
        context = ToolContext.from_env(dry_run=True, cwd=Path.cwd(), env={})
        page = SimpleNamespace(folders=({"FID": "1234567", "UID": "private", "name": "稍後閱讀"},))
        collection = SimpleNamespace(items=(_jm_favorite("1"),), pages_fetched=1)
        client = SimpleNamespace(
            get_favorites_page=MagicMock(return_value=page),
            collect_favorites=MagicMock(return_value=collection),
        )
        with patch.object(comic_tools, "_jm_client", return_value=client):
            result = asyncio.run(
                comic_tools.jmcomic_favorites_collect(context, {"folders": ["稍後閱讀"]})
            )
            discovered = asyncio.run(comic_tools.jmcomic_favorite_folders_collect(context, {}))

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["folders"][0]["folder_id"], "1234567")
        client.collect_favorites.assert_called_once_with(folder_id="1234567")
        self.assertTrue(discovered.is_success, discovered.to_dict())
        self.assertEqual(
            [(folder["name"], folder["folder_id"]) for folder in discovered.data["folders"]],
            [("all", "0"), ("稍後閱讀", "1234567")],
        )

    def test_jmcomic_folder_selection_rejects_empty_or_unknown_names(self) -> None:
        context = ToolContext.from_env(dry_run=True, cwd=Path.cwd(), env={})
        client = SimpleNamespace(
            collect_favorites=MagicMock(),
            get_favorites_page=MagicMock(return_value=SimpleNamespace(folders=())),
        )
        with patch.object(comic_tools, "_jm_client", return_value=client):
            empty = asyncio.run(comic_tools.jmcomic_favorites_collect(context, {"folders": []}))
            unknown = asyncio.run(
                comic_tools.jmcomic_favorites_collect(context, {"folders": ["not-registered"]})
            )

        self.assertFalse(empty.is_success)
        self.assertEqual(empty.error.code, "jmcomic_folders_empty")
        self.assertFalse(unknown.is_success)
        self.assertEqual(unknown.error.code, "jmcomic_folder_unknown")
        client.collect_favorites.assert_not_called()

    def test_jmcomic_multi_folder_sync_commits_union_and_selection_change_stops_unique_follow(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(database),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "JMCOMIC_USERNAME": "configured-user",
                },
            )
            db.initialize_database(database)
            account_key = comic_tools._jm_account_collection_key(context)
            for name, folder_id in (("Folder A", "101"), ("Folder B", "202")):
                db.register_collection_scope_alias(
                    database,
                    provider="jmcomic",
                    account_key=account_key,
                    scope_kind="favorite_folder",
                    scope_name=name,
                    scope_name_key=name.casefold(),
                    remote_id=folder_id,
                )

            def favorite(album_id: str):
                return SimpleNamespace(
                    album_id=album_id,
                    provider_work_id=f"album:{album_id}",
                    title=f"Album {album_id}",
                    latest_photo_id=None,
                )

            folder_items = {
                "101": (favorite("1"), favorite("2")),
                "202": (favorite("2"), favorite("3")),
            }
            client = SimpleNamespace(
                session=JMComicSession({"AVS": "saved"}, username="configured-user"),
                collect_favorites=MagicMock(
                    side_effect=lambda *, folder_id="0": SimpleNamespace(
                        items=folder_items[folder_id],
                        pages_fetched=1,
                    )
                ),
            )
            with patch.object(comic_tools, "_jm_client", return_value=client), patch.object(
                comic_tools,
                "_sync_favorite_targets",
                new=AsyncMock(
                    side_effect=lambda *_: ToolResult.success({"summary": {}, "items": [], "packages": []})
                ),
            ):
                first = asyncio.run(
                    comic_tools.jmcomic_favorites_sync(context, {"folders": ["Folder A", "Folder B"]})
                )
                second = asyncio.run(
                    comic_tools.jmcomic_favorites_sync(context, {"folders": ["Folder A"]})
                )

            collection_key = comic_tools._jm_account_collection_key(context)
            active = db.list_collection_memberships(
                database,
                provider="jmcomic",
                collection_key=collection_key,
            )
            inactive = db.list_collection_memberships(
                database,
                provider="jmcomic",
                collection_key=collection_key,
                active=False,
            )

        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(first.data["favorites_seen"], 3)
        self.assertEqual(first.data["folder_memberships_seen"], 4)
        self.assertEqual(first.data["snapshot"]["added"], 3)
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(second.data["snapshot"]["removed"], 1)
        self.assertEqual([row["target_id"] for row in active], ["1", "2"])
        self.assertEqual([row["target_id"] for row in inactive], ["3"])
        album_two = next(row for row in active if row["target_id"] == "2")
        self.assertEqual([folder["name"] for folder in album_two["metadata"]["favorite_folders"]], ["Folder A"])
        self.assertEqual(
            [call.kwargs["folder_id"] for call in client.collect_favorites.call_args_list],
            ["101", "202", "101"],
        )

    def test_jmcomic_multi_folder_failure_preserves_previous_snapshot(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(database),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "JMCOMIC_USERNAME": "configured-user",
                },
            )
            db.initialize_database(database)
            collection_key = comic_tools._jm_account_collection_key(context)
            db.commit_collection_snapshot(
                database,
                provider="jmcomic",
                collection_key=collection_key,
                targets=[{"target_type": "album", "target_id": "old"}],
            )
            for name, folder_id in (("Folder A", "101"), ("Folder B", "202")):
                db.register_collection_scope_alias(
                    database,
                    provider="jmcomic",
                    account_key=collection_key,
                    scope_kind="favorite_folder",
                    scope_name=name,
                    scope_name_key=name.casefold(),
                    remote_id=folder_id,
                )
            favorite = SimpleNamespace(
                album_id="new",
                provider_work_id="album:new",
                title="New",
                latest_photo_id=None,
            )

            def collect(*, folder_id="0"):
                if folder_id == "202":
                    raise JMComicClientError("jmcomic_collection_incomplete", "incomplete")
                return SimpleNamespace(items=(favorite,), pages_fetched=1)

            client = SimpleNamespace(
                session=JMComicSession({"AVS": "saved"}, username="configured-user"),
                collect_favorites=MagicMock(side_effect=collect),
                login=MagicMock(),
            )
            with patch.object(comic_tools, "_jm_client", return_value=client):
                result = asyncio.run(
                    comic_tools.jmcomic_favorites_sync(context, {"folders": ["Folder A", "Folder B"]})
                )
            active = db.list_collection_memberships(
                database,
                provider="jmcomic",
                collection_key=collection_key,
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "jmcomic_collection_incomplete")
        self.assertEqual([row["target_id"] for row in active], ["old"])
        client.login.assert_not_called()

    def test_jmcomic_favorites_collect_recovers_expired_session_once(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_path = root / "jmcomic-session.json"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "JMCOMIC_USERNAME": "configured-user",
                    "JMCOMIC_PASSWORD": "configured-password",
                    "JMCOMIC_SESSION_FILE": str(session_path),
                },
            )
            collection = SimpleNamespace(pages_fetched=1, total=1, items=(_jm_favorite("1"),))
            client = SimpleNamespace(
                session=JMComicSession({"AVS": "expired"}, username="configured-user")
            )

            def login(*, username: str, password: str) -> JMComicSession:
                self.assertEqual((username, password), ("configured-user", "configured-password"))
                client.session = JMComicSession({"AVS": "fresh"}, username=username)
                return client.session

            client.login = MagicMock(side_effect=login)
            client.collect_favorites = MagicMock(
                side_effect=[
                    JMComicClientError("jmcomic_auth_required", "expired"),
                    collection,
                ]
            )
            with patch.object(comic_tools, "_jm_client", return_value=client):
                result = asyncio.run(comic_tools.jmcomic_favorites_collect(context, {}))

            saved = comic_tools.jm_auth.load_session(
                env=context.env,
                cwd=root,
                session_file=str(session_path),
            )

        self.assertTrue(result.is_success, result.to_dict())
        self.assertTrue(result.data["auth_recovery_attempted"])
        self.assertTrue(result.data["auth_recovered"])
        self.assertTrue(result.data["session_checkpointed"])
        self.assertEqual(result.data["session_checkpoints"], 2)
        self.assertEqual(saved.cookies, {"AVS": "fresh"})
        client.login.assert_called_once()
        self.assertEqual(client.collect_favorites.call_count, 2)

    def test_jmcomic_auth_recovery_does_not_loop_after_retry_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "JMCOMIC_USERNAME": "configured-user",
                    "JMCOMIC_PASSWORD": "configured-password",
                },
            )
            client = SimpleNamespace(session=JMComicSession({"AVS": "expired"}))

            def login(*, username: str, password: str) -> JMComicSession:
                client.session = JMComicSession({"AVS": "fresh"}, username=username)
                return client.session

            client.login = MagicMock(side_effect=login)
            client.collect_favorites = MagicMock(
                side_effect=JMComicClientError("jmcomic_auth_required", "still expired")
            )
            with patch.object(comic_tools, "_jm_client", return_value=client):
                result = asyncio.run(comic_tools.jmcomic_favorites_collect(context, {}))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "jmcomic_auth_required")
        self.assertTrue(result.data["auth_recovery_attempted"])
        self.assertTrue(result.data["auth_recovered"])
        client.login.assert_called_once()
        self.assertEqual(client.collect_favorites.call_count, 2)

    def test_jmcomic_auth_recovery_does_not_retry_non_auth_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "JMCOMIC_USERNAME": "configured-user",
                    "JMCOMIC_PASSWORD": "configured-password",
                },
            )
            client = SimpleNamespace(
                session=JMComicSession({"AVS": "current"}),
                login=MagicMock(),
                collect_favorites=MagicMock(
                    side_effect=JMComicClientError("jmcomic_response_invalid", "bad response")
                ),
            )
            with patch.object(comic_tools, "_jm_client", return_value=client):
                result = asyncio.run(comic_tools.jmcomic_favorites_collect(context, {}))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "jmcomic_response_invalid")
        self.assertFalse(result.data["auth_recovery_attempted"])
        client.login.assert_not_called()

    def test_jmcomic_favorites_sync_checkpoints_session_after_target_resolution(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_path = root / "jmcomic-session.json"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(root / "state.sqlite3"),
                    "MEDIAGENT_LIBRARY_DIR": str(root / "library"),
                    "JMCOMIC_USERNAME": "configured-user",
                    "JMCOMIC_PASSWORD": "configured-password",
                    "JMCOMIC_SESSION_FILE": str(session_path),
                },
            )
            favorite = SimpleNamespace(
                album_id="1",
                provider_work_id="album:1",
                title="test album",
                latest_photo_id="10",
            )
            collection = SimpleNamespace(items=(favorite,), pages_fetched=1)
            resolution = SimpleNamespace(normalized_items=lambda: [_comic_item()])
            client = SimpleNamespace(
                session=JMComicSession({"AVS": "before-resolve"}, username="configured-user"),
                login=MagicMock(),
                collect_favorites=MagicMock(return_value=collection),
            )

            def resolve_exact(url: str):
                self.assertEqual(url, "https://18comic.vip/album/1/")
                client.session = JMComicSession({"AVS": "after-resolve"}, username="configured-user")
                return resolution

            client.resolve_exact = MagicMock(side_effect=resolve_exact)
            sync_result = ToolResult.success({"summary": {}, "items": [], "packages": []})
            with patch.object(comic_tools, "_jm_client", return_value=client), patch.object(
                comic_tools,
                "_sync_items",
                new=AsyncMock(return_value=sync_result),
            ):
                result = asyncio.run(comic_tools.jmcomic_favorites_sync(context, {}))

            saved = comic_tools.jm_auth.load_session(
                env=context.env,
                cwd=root,
                session_file=str(session_path),
            )

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(saved.cookies, {"AVS": "after-resolve"})
        self.assertEqual(result.data["session_checkpoints"], 2)
        client.resolve_exact.assert_called_once()

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


def _jm_favorite(album_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        album_id=album_id,
        provider_work_id=f"album:{album_id}",
        title=f"Album {album_id}",
        latest_photo_id=None,
    )


def _jmcomic_item_with_spacer() -> dict:
    return {
        "platform": "jmcomic",
        "remote_id": "photo:1234567",
        "source_url": "https://18comic.vip/photo/1234567/",
        "media_type": "photo",
        "status": "discovered",
        "source_availability": "available",
        "source_timestamp": "2026-01-02T00:00:00+00:00",
        "metadata": {
            "title": "JMComic Spacer Example",
            "work_type": "comic",
            "storage_category": "comic-pages",
            "page_count": 2,
            "comic": {
                "provider": "jmcomic",
                "provider_work_id": "photo:1234567",
                "title": "JMComic Spacer Example",
                "is_one_shot": True,
                "total_count": 1,
            },
            "files": [
                {
                    "url": "https://1.1.1.1/00001.webp",
                    "kind": "image",
                    "page": 0,
                    "storage_category": "comic-pages",
                    "runtime_decode": {
                        "provider": "jmcomic",
                        "vertical_segments": 8,
                    },
                },
                {
                    "url": "https://1.1.1.1/00002.webp",
                    "kind": "image",
                    "page": 1,
                    "storage_category": "comic-pages",
                    "runtime_decode": {
                        "provider": "jmcomic",
                        "vertical_segments": 8,
                    },
                },
            ],
        },
    }


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    return output.getvalue()


def _webp_bytes(width: int, height: int, color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="WEBP")
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
