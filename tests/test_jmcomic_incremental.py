from __future__ import annotations

import asyncio
import hashlib
import io
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from mediagent.core import comic_sync_state as state, db
from mediagent.core.comics import CBZ_MIME_TYPE, comic_archive_relative_path
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext, ToolResult
from mediagent.platforms.jmcomic.auth import JMComicSession
from mediagent.platforms.jmcomic.client import JMComicClientError, JMComicResolution
from mediagent.platforms.jmcomic.incremental import chapter_manifest, local_health
from mediagent.platforms.jmcomic.links import parse_jmcomic_link
from mediagent.platforms.jmcomic.parser import JMComicAlbum, JMComicEpisode, JMComicPage, JMComicPhoto
from mediagent.tools import comic_tools


def album(album_id="1", chapter_ids=("10", "20")):
    return JMComicAlbum(str(album_id), f"Album {album_id}", tuple(
        JMComicEpisode(str(chapter_id), index, f"Chapter {index}", position=index)
        for index, chapter_id in enumerate(chapter_ids, 1)
    ))


def photo(chapter_id, album_id="1"):
    return JMComicPhoto(str(chapter_id), str(album_id), "A chapter", 1, (
        JMComicPage(0, "1.png", f"https://cdn.example/{chapter_id}/1.png", "99999999"),
    ))


class IncrementalFixture:
    def __init__(self, root, albums, *, dry_run=False):
        self.root = Path(root)
        self.database = self.root / "state.sqlite3"
        self.library = self.root / "library"
        self.library.mkdir()
        self.albums = {item.album_id: item for item in albums}
        self.context = ToolContext.from_env(cwd=self.root, dry_run=dry_run, env={
            "MEDIAGENT_DATA_DIR": str(root), "MEDIAGENT_DB_PATH": str(self.database),
            "MEDIAGENT_LIBRARY_DIR": str(self.library), "JMCOMIC_USERNAME": "fixture-user",
        })
        self.client = SimpleNamespace(
            session=JMComicSession({"AVS": "offline"}, username="fixture-user"),
            get_album=MagicMock(side_effect=lambda value: self.albums[str(value)]),
            get_scramble_id=MagicMock(return_value="99999999"),
            get_photo=MagicMock(side_effect=lambda chapter_id, **_: photo(chapter_id, next(item.album_id for item in self.albums.values() if any(ep.photo_id == chapter_id for ep in item.episodes)))),
        )
        self.recovery = SimpleNamespace(run=lambda operation: operation())
        self.targets = [{"target_id": item.album_id, "target_type": "album", "metadata": {"latest_photo_id": item.episodes[-1].photo_id}} for item in albums]
        self.account = comic_tools._jm_account_collection_key(self.context, self.client)
        db.initialize_database(self.database)

    def scope(self, album_id="1"):
        return {"provider": "jmcomic", "account_key": self.account, "album_id": album_id}

    def checkpoint(self, item=None):
        item = item or next(iter(self.albums.values()))
        return state.save_manifest(self.database, **self.scope(item.album_id), latest_photo_id=item.episodes[-1].photo_id, episodes=chapter_manifest(item))

    def seed(self):
        """Bulk insert an authoritative, healthy legacy library (no checkpoints)."""
        source = self.library / "fixture-page.png"
        image = io.BytesIO()
        Image.new("RGB", (2, 2), (128, 64, 32)).save(image, "PNG")
        source.write_bytes(image.getvalue())
        with db.connect(self.database) as connection:
            for item in self.albums.values():
                for episode in item.episodes:
                    normalized = JMComicResolution(parse_jmcomic_link(f"https://18comic.vip/album/{item.album_id}/"), item, (photo(episode.photo_id, item.album_id),)).normalized_items()[0]
                    cursor = connection.execute("INSERT INTO media_items(platform,remote_id,source_url,media_type,status,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", ("jmcomic", normalized["remote_id"], normalized["source_url"], "photo", "downloaded", json.dumps(normalized["metadata"]), "2020", "2020"))
                    archive = self.library / comic_archive_relative_path(item=normalized, include_platform_layer=True)
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    archive.write_bytes(b"synthetic archive bytes")
                    for key, remote, path, mime in (("page:000001", f"https://cdn.example/{episode.photo_id}/1.png", source, "image/png"), ("archive:cbz", None, archive, CBZ_MIME_TYPE)):
                        connection.execute("INSERT INTO media_files(media_item_id,file_key,remote_url,local_path,mime_type,size_bytes,checksum,status,file_health,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cursor.lastrowid, key, remote, str(path), mime, path.stat().st_size, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), "downloaded", "valid", "2020", "2020"))

    def run(self, **options):
        return asyncio.run(comic_tools._sync_jm_incremental_targets(self.context, options, self.targets, self.client, self.recovery))

    def row(self, chapter_id="10", album_id="1"):
        return next(row for row in state.read_album(self.database, **self.scope(album_id))["chapters"] if row["chapter_id"] == chapter_id)


class JMComicIncrementalTests(unittest.TestCase):
    def test_healthy_12_by_100_uses_only_album_requests(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(str(number), tuple(str(number * 1000 + index) for index in range(1, 101))) for number in range(1, 13)])
            fixture.seed()
            for _ in range(2):
                fixture.client.get_album.reset_mock()
                result = fixture.run()
                self.assertTrue(result.is_success, result.to_dict())
                self.assertEqual(result.data["summary"]["chapters_healthy"], 1200)
                self.assertEqual(fixture.client.get_album.call_count, 12)
                fixture.client.get_photo.assert_not_called()
                fixture.client.get_scramble_id.assert_not_called()
            self.assertIsNone(fixture.row("1001", "1")["last_verified_at"])

    def test_lagging_latest_hint_does_not_hide_mid_sequence_new_chapter(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.seed()
            fixture.run()
            fixture.albums["1"] = album(chapter_ids=("10", "15", "20"))
            with patch.object(comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))) as sync:
                result = fixture.run(max_chapters=1)
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(fixture.client.get_photo.call_args.args[0], "15")
            normalized = sync.call_args.args[2][0]
            self.assertEqual(normalized["metadata"]["comic"]["total_count"], 3)
            self.assertEqual(normalized["metadata"]["comic"]["chapter_number"], 2)
            self.assertTrue(result.data["budget_exhausted"])

    def test_missing_cbz_repairs_from_local_pages_without_remote_chapter(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.seed()
            with db.connect(fixture.database) as connection:
                archive = connection.execute("SELECT local_path FROM media_files WHERE mime_type=?", (CBZ_MIME_TYPE,)).fetchone()[0]
            Path(archive).unlink()
            result = fixture.run()
            self.assertTrue(result.is_success, result.to_dict())
            fixture.client.get_photo.assert_not_called()
            self.assertTrue(Path(archive).exists())
            self.assertEqual(fixture.row()["status"], "succeeded")
            self.assertEqual(result.data["summary"]["chapters_local_repair"], 1)

    def test_no_repair_retains_failed_checkpoint_without_media_and_downloads_new(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.checkpoint()
            lease = state.claim_chapter(fixture.database, **fixture.scope(), chapter_id="10", owner="fixture")
            state.finish_chapter(fixture.database, **fixture.scope(), chapter_id="10", token=lease["token"], generation=lease["generation"], status="failed", error_code="jmcomic_request_failed", now=datetime.now(UTC) - timedelta(days=1))
            with patch.object(comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))):
                result = fixture.run(retry_failed=False, repair_missing_files=False)
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual([call.args[0] for call in fixture.client.get_photo.call_args_list], ["20"])
            self.assertEqual(fixture.row()["status"], "failed")

    def test_full_revalidation_resumes_bounded_pass_and_does_not_redownload_healthy_bytes(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10", "20", "30"))])
            fixture.seed()
            fixture.run()
            with patch.object(comic_tools.link_tools, "download_resolved_file_safely", new=AsyncMock()) as download:
                first = fixture.run(revalidate_all=True, max_chapters=1)
                second = fixture.run(revalidate_all=True, max_chapters=1)
                third = fixture.run(revalidate_all=True, max_chapters=1)
            for result in (first, second, third):
                self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual([call.args[0] for call in fixture.client.get_photo.call_args_list], ["10", "20", "30"])
            download.assert_not_called()
            current = state.read_album(fixture.database, **fixture.scope())
            self.assertEqual(current["completed_generation"], current["generation"])
            self.assertFalse(current["revalidation_active"])

    def test_expired_running_chapter_is_reclaimed(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.checkpoint()
            state.claim_chapter(fixture.database, **fixture.scope(), chapter_id="10", owner="crashed", lease_seconds=1, now=datetime.now(UTC) - timedelta(days=1))
            with patch.object(comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))):
                result = fixture.run()
            self.assertTrue(result.is_success, result.to_dict())
            fixture.client.get_photo.assert_called_once()
            self.assertEqual(fixture.row()["status"], "succeeded")

    def test_failed_photo_retains_prior_success_and_continues_later_chapters(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10", "20", "30"))])
            fixture.client.get_photo.side_effect = lambda chapter_id, **_: (_ for _ in ()).throw(JMComicClientError("jmcomic_request_failed", "offline")) if chapter_id == "20" else photo(chapter_id)
            with patch.object(comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))):
                result = fixture.run()
            self.assertFalse(result.is_success)
            self.assertEqual(fixture.row("10")["status"], "succeeded")
            self.assertEqual(fixture.row("20")["status"], "failed")
            self.assertEqual(fixture.row("30")["status"], "succeeded")

    def test_cooperative_pause_retains_pending_and_avoids_false_completion(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            with patch.object(comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}, "budget_exhausted": True}))):
                result = fixture.run()
            self.assertTrue(result.is_success, result.to_dict())
            self.assertTrue(result.data["budget_exhausted"])
            self.assertEqual(fixture.row()["status"], "paused")
            self.assertIsNone(state.read_album(fixture.database, **fixture.scope())["completed_generation"])

    def test_rotating_albums_avoid_first_large_album_starving_following_album(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album("1", ("10", "20")), album("2", ("30", "40"))])
            with patch.object(comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))):
                fixture.run(max_chapters=1)
                fixture.run(max_chapters=1)
            self.assertEqual([call.args[0] for call in fixture.client.get_photo.call_args_list], ["10", "30"])

    def test_audit_is_bounded_and_advances_fair_cursor(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10", "20", "30"))])
            fixture.seed()
            fixture.run()
            for expected in ("10", "20"):
                with db.connect(fixture.database) as connection:
                    connection.execute("UPDATE comic_sync_albums SET last_audit_at=?", ((datetime.now(UTC) - timedelta(days=2)).isoformat(),))
                result = fixture.run(audit_chapters=1)
                self.assertTrue(result.is_success, result.to_dict())
                self.assertEqual(fixture.client.get_photo.call_args.args[0], expected)
                self.assertEqual(result.data["summary"]["chapters_audited"], 1)

    def test_removed_content_is_not_revived_by_revalidation_or_overwrite(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.seed()
            with db.connect(fixture.database) as connection:
                connection.execute("UPDATE media_files SET file_health='purged'")
            result = fixture.run(revalidate_all=True, overwrite=True)
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(result.data["summary"]["chapters_suppressed"], 1)
            fixture.client.get_photo.assert_not_called()

    def test_deep_health_rejects_outside_symlink_before_checksum_read(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.seed()
            item = comic_tools._load_comic_items(fixture.database, {("jmcomic", "photo:10")})[0]
            outside = fixture.root / "outside.png"
            outside.write_bytes(b"outside library")
            page = fixture.library / "fixture-page.png"
            page.unlink()
            page.symlink_to(outside)
            with self.assertRaises(ValueError):
                local_health(item, fixture.library, deep=True)

    def test_normal_no_repair_run_resumes_a_budget_paused_new_chapter(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            clock = SimpleNamespace(value=0)
            fixture.client.get_photo.side_effect = None
            fixture.client.get_photo.return_value = JMComicPhoto("10", "1", "chapter", 1, (
                JMComicPage(0, "1.png", "https://1.1.1.1/10/1.png"),
                JMComicPage(1, "2.png", "https://1.1.1.1/10/2.png"),
            ))
            image = io.BytesIO()
            Image.new("RGB", (3, 3), (30, 80, 120)).save(image, "PNG")
            calls = []

            class Http:
                def get_limited(self, url, **kwargs):
                    calls.append(url)
                    clock.value = 2
                    return HttpResponse(200, {"Content-Type": "image/png"}, image.getvalue(), url)

            fixture.context.http_client = Http()
            with patch.object(comic_tools, "time", SimpleNamespace(monotonic=lambda: clock.value)), patch.object(comic_tools.link_tools, "time", SimpleNamespace(monotonic=lambda: clock.value)):
                first = fixture.run(time_budget_seconds=1, retry_failed=False, repair_missing_files=False)
                self.assertTrue(first.is_success, first.to_dict())
                self.assertTrue(first.data["budget_exhausted"])
                self.assertEqual(fixture.row()["status"], "paused")
                self.assertFalse(first.data["packages"])
                clock.value = 0
                second = fixture.run(time_budget_seconds=1, retry_failed=False, repair_missing_files=False)
            self.assertTrue(second.is_success, second.to_dict())
            self.assertEqual(fixture.row()["status"], "succeeded")
            self.assertEqual(calls, ["https://1.1.1.1/10/1.png", "https://1.1.1.1/10/2.png"])
            self.assertEqual(second.data["summary"]["cbz_packaged"], 1)

    def test_completed_old_chapters_do_not_consume_budget_before_new_append(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=tuple(str(index) for index in range(1, 101)))])
            fixture.seed()
            fixture.run()
            fixture.albums["1"] = album(chapter_ids=tuple(str(index) for index in range(1, 102)))
            with patch.object(comic_tools, "_sync_items", new=AsyncMock(return_value=ToolResult.success({"summary": {}}))):
                fixture.run()
            self.assertEqual(fixture.client.get_photo.call_count, 1)
            self.assertEqual(fixture.client.get_photo.call_args.args[0], "101")

    def test_changed_fingerprint_resolves_even_with_healthy_cached_pages(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.seed()
            fixture.run()
            fixture.albums["1"] = JMComicAlbum("1", "Album 1", (JMComicEpisode("10", 1, "Renamed chapter", position=1), JMComicEpisode("20", 2, "Chapter 2", position=2)))
            result = fixture.run()
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual([call.args[0] for call in fixture.client.get_photo.call_args_list], ["10"])

    def test_dry_run_full_revalidation_resolves_without_checkpoint_writes(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.seed()
            fixture.run()
            before = fixture.database.read_bytes()
            fixture.context.dry_run = True
            result = fixture.run(revalidate_all=True)
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(fixture.client.get_photo.call_count, 2)
            self.assertEqual(fixture.database.read_bytes(), before)

    def test_normal_run_continues_deep_checks_for_active_full_pass(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.seed()
            fixture.run()
            fixture.run(revalidate_all=True, max_chapters=1)
            with patch.object(comic_tools.jm_incremental, "local_health", wraps=local_health) as health:
                result = fixture.run(max_chapters=1)
            self.assertTrue(result.is_success, result.to_dict())
            self.assertTrue(any(call.kwargs.get("deep") for call in health.call_args_list))
            self.assertFalse(state.read_album(fixture.database, **fixture.scope())["revalidation_active"])

    def test_obsolete_page_record_cannot_mask_missing_current_page(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.seed()
            item = comic_tools._load_comic_items(fixture.database, {("jmcomic", "photo:10")})[0]
            item["metadata"]["files"][0]["url"] = "https://cdn.example/new-page.png"
            item["metadata"]["files"][0]["page"] = 9
            self.assertFalse(local_health(item, fixture.library)["complete"])

    def test_all_spacer_skipped_chapter_is_adopted_without_remote_retry(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.seed()
            with db.connect(fixture.database) as connection:
                connection.execute("UPDATE media_items SET status='skipped'")
                connection.execute("DELETE FROM media_files WHERE mime_type=?", (CBZ_MIME_TYPE,))
                connection.execute("UPDATE media_files SET file_health='ignored_spacer',status='skipped',local_path=NULL")
            result = fixture.run()
            self.assertTrue(result.is_success, result.to_dict())
            fixture.client.get_photo.assert_not_called()
            self.assertEqual(fixture.row()["status"], "succeeded")

    def test_budget_deferred_manifest_change_still_requires_remote_next_run(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.seed()
            fixture.run()
            fixture.albums["1"] = album(chapter_ids=("10", "15", "20"))
            async def saved_item(context, options, items):
                for item in items:
                    db.upsert_media_item(fixture.database, item)
                return ToolResult.success({"summary": {}})

            with patch.object(comic_tools, "_sync_items", new=AsyncMock(side_effect=saved_item)):
                first = fixture.run(max_chapters=1)
                second = fixture.run(max_chapters=1)
            self.assertTrue(first.is_success, first.to_dict())
            self.assertTrue(second.is_success, second.to_dict())
            self.assertEqual([call.args[0] for call in fixture.client.get_photo.call_args_list], ["15", "20"])
            self.assertIsNotNone(fixture.row("20")["last_verified_at"])

    def test_inserted_chapter_updates_comicinfo_and_preserves_existing_archive_path(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.seed()
            fixture.run()
            prior_files = db.list_media_files(fixture.database, platform="jmcomic", remote_id="photo:20")
            old_archive = Path(next(record["local_path"] for record in prior_files if record["mime_type"] == CBZ_MIME_TYPE))
            old_bytes = old_archive.read_bytes()
            fixture.albums["1"] = album(chapter_ids=("10", "15", "20"))
            fixture.client.get_photo.side_effect = lambda chapter_id, **_: JMComicPhoto(chapter_id, "1", "chapter", 1, (JMComicPage(0, "1.png", f"https://1.1.1.1/{chapter_id}/1.png"),))
            page_bytes = (fixture.library / "fixture-page.png").read_bytes()
            fixture.context.http_client = SimpleNamespace(get_limited=lambda url, **_: HttpResponse(200, {"Content-Type": "image/png"}, page_bytes, url))
            first = fixture.run(max_chapters=1)
            second = fixture.run(max_chapters=1)
            self.assertTrue(first.is_success, first.to_dict())
            self.assertTrue(second.is_success, second.to_dict())
            archives = db.list_media_files(fixture.database, platform="jmcomic", remote_id="photo:20")
            archive = Path(next(record["local_path"] for record in archives if record["mime_type"] == CBZ_MIME_TYPE))
            self.assertTrue(archive.exists())
            self.assertEqual(archive, old_archive)
            self.assertNotEqual(archive.read_bytes(), old_bytes)
            from zipfile import ZipFile
            with ZipFile(archive) as packaged:
                self.assertIn("<Number>3</Number>", packaged.read("ComicInfo.xml").decode())
            self.assertEqual([call.args[0] for call in fixture.client.get_photo.call_args_list], ["15", "20"])
            self.assertEqual(fixture.row("20")["status"], "succeeded")

    def test_multi_album_full_pass_does_not_restart_completed_album_while_others_pending(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album("1", ("10",)), album("2", ("20",))])
            fixture.seed()
            fixture.run()
            first = fixture.run(revalidate_all=True, max_chapters=1)
            second = fixture.run(revalidate_all=True, max_chapters=1)
            self.assertTrue(first.is_success, first.to_dict())
            self.assertTrue(second.is_success, second.to_dict())
            self.assertTrue(first.data["revalidation_active"])
            self.assertFalse(second.data["revalidation_active"])
            self.assertEqual(second.data["summary"]["albums_pending"], 0)
            self.assertEqual([call.args[0] for call in fixture.client.get_photo.call_args_list], ["10", "20"])

    def test_full_pass_intent_before_any_manifest_survives_next_normal_run(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album("1", ("10",)), album("2", ("20",))])
            fixture.seed()
            with patch.object(comic_tools, "time", SimpleNamespace(monotonic=MagicMock(side_effect=[0, 2]))):
                paused = fixture.run(revalidate_all=True, time_budget_seconds=1)
            self.assertTrue(paused.is_success, paused.to_dict())
            self.assertEqual(fixture.client.get_album.call_count, 0)
            self.assertEqual(paused.data["summary"]["albums_pending"], 2)
            with patch.object(comic_tools.jm_incremental, "local_health", wraps=local_health) as health:
                resumed = fixture.run()
            self.assertTrue(resumed.is_success, resumed.to_dict())
            self.assertEqual(fixture.client.get_photo.call_count, 2)
            self.assertEqual(sum(bool(call.kwargs.get("deep")) for call in health.call_args_list), 2)
            self.assertFalse(resumed.data["revalidation_active"])

    def test_atomic_archive_refresh_failure_keeps_original_path_and_can_retry(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album()])
            fixture.seed()
            fixture.run()
            old_archive = Path(next(record["local_path"] for record in db.list_media_files(fixture.database, platform="jmcomic", remote_id="photo:20") if record["mime_type"] == CBZ_MIME_TYPE))
            old_bytes = old_archive.read_bytes()
            fixture.albums["1"] = album(chapter_ids=("20", "10"))
            from mediagent.tools import pixiv_library_tools
            with patch.object(pixiv_library_tools, "build_cbz_atomic", side_effect=OSError("synthetic build failure")):
                failed = fixture.run(max_chapters=1)
            self.assertFalse(failed.is_success)
            self.assertTrue(old_archive.exists())
            self.assertEqual(old_archive.read_bytes(), old_bytes)
            self.assertEqual(fixture.row("20")["status"], "failed")
            with db.connect(fixture.database) as connection:
                connection.execute("UPDATE comic_sync_chapters SET next_attempt_at=NULL WHERE chapter_id='20'")
            succeeded = fixture.run()
            self.assertTrue(succeeded.is_success, succeeded.to_dict())
            self.assertTrue(old_archive.exists())
            from zipfile import ZipFile
            with ZipFile(old_archive) as packaged:
                self.assertIn("<Number>1</Number>", packaged.read("ComicInfo.xml").decode())
            self.assertEqual(fixture.row("20")["status"], "succeeded")

    def test_rotated_cdn_url_stable_page_identity_survives_metadata_only_repack(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.seed()
            fixture.run()
            fixture.client.get_photo.side_effect = None
            fixture.client.get_photo.return_value = JMComicPhoto("10", "1", "A chapter", 1, (JMComicPage(0, "1.png", "https://rotated.example/10/1.png"),))
            with patch.object(comic_tools.link_tools, "download_resolved_file_safely", new=AsyncMock()) as download:
                checked = fixture.run(revalidate_all=True)
                self.assertTrue(checked.is_success, checked.to_dict())
                original = fixture.albums["1"]
                fixture.albums["1"] = JMComicAlbum("1", "Changed album title", original.episodes)
                repacked = fixture.run()
            self.assertTrue(repacked.is_success, repacked.to_dict())
            self.assertEqual(repacked.data["summary"]["chapters_metadata_refresh"], 1)
            self.assertEqual(repacked.data["summary"]["cbz_packaged"], 1)
            download.assert_not_called()
            self.assertEqual(fixture.client.get_photo.call_count, 1)

    def test_full_revalidation_replaces_corrupt_page_bytes_only(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album(chapter_ids=("10",))])
            fixture.client.get_photo.side_effect = None
            fixture.client.get_photo.return_value = JMComicPhoto("10", "1", "chapter", 1, (JMComicPage(0, "1.png", "https://1.1.1.1/10/1.png"),))
            image = io.BytesIO()
            Image.new("RGB", (3, 3), (90, 60, 120)).save(image, "PNG")
            calls = []

            class Http:
                def get_limited(self, url, **kwargs):
                    calls.append(url)
                    return HttpResponse(200, {"Content-Type": "image/png"}, image.getvalue(), url)

            fixture.context.http_client = Http()
            first = fixture.run()
            self.assertTrue(first.is_success, first.to_dict())
            records = db.list_media_files(fixture.database, platform="jmcomic", remote_id="photo:10")
            page = next(record for record in records if record["file_key"] == "page:000001")
            Path(page["local_path"]).write_bytes(b"corrupted page")
            checked = fixture.run(revalidate_all=True)
            self.assertTrue(checked.is_success, checked.to_dict())
            self.assertEqual(Path(page["local_path"]).read_bytes(), image.getvalue())
            self.assertEqual(len(calls), 2)
            self.assertEqual(checked.data["summary"]["repair_files_corrupt"], 1)

    def test_deadline_gates_manifest_requests_for_later_albums(self):
        with TemporaryDirectory() as root:
            fixture = IncrementalFixture(root, [album("1", ("10",)), album("2", ("20",))])
            with patch.object(comic_tools, "time", SimpleNamespace(monotonic=MagicMock(side_effect=[0, 0, 2, 2]))):
                result = fixture.run(time_budget_seconds=1)
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(fixture.client.get_album.call_count, 1)
            fixture.client.get_photo.assert_not_called()
            self.assertEqual(result.data["summary"]["albums_pending"], 2)


if __name__ == "__main__":
    unittest.main()
