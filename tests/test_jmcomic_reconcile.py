from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image

from mediagent.core import db
from mediagent.core.comics import CBZ_MIME_TYPE, build_cbz_atomic, comic_archive_relative_path
from mediagent.core.tooling import ToolContext
from mediagent.platforms.jmcomic.parser import parse_album
from mediagent.tools import comic_tools, jmcomic_reconcile


class JMComicReconcileTests(unittest.TestCase):
    def test_tool_plan_is_read_only_and_apply_requires_confirmation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, library, old_archive, resolved = _seed_wrong_chapter(root)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(database),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                },
            )
            album = parse_album(
                {
                    "id": "1215913",
                    "name": "飛機杯女神連線中",
                    "series": [{"id": "1463677", "sort": "74", "name": "CH74"}],
                }
            )
            client = SimpleNamespace(get_album=lambda _album_id: album)
            with patch.object(comic_tools, "_jm_client", return_value=client):
                planned = asyncio.run(
                    comic_tools.jmcomic_library_reconcile(
                        context,
                        {"mode": "plan", "album_id": "1215913"},
                    )
                )
                unconfirmed = asyncio.run(
                    comic_tools.jmcomic_library_reconcile(
                        context,
                        {"mode": "apply", "album_id": "1215913"},
                    )
                )
            self.assertTrue(planned.is_success)
            self.assertEqual(planned.data["summary"]["archives_to_rebuild"], 1)
            self.assertTrue(old_archive.is_file())
            self.assertFalse(unconfirmed.is_success)
            self.assertEqual(unconfirmed.error.code, "jmcomic_reconcile_confirmation_required")
            self.assertTrue(old_archive.is_file())
            with patch.object(comic_tools, "_jm_client", return_value=client):
                confirmed = asyncio.run(
                    comic_tools.jmcomic_library_reconcile(
                        context,
                        {"mode": "apply", "album_id": "1215913", "confirm": True},
                    )
                )
            self.assertTrue(confirmed.is_success, confirmed.to_dict())
            self.assertEqual(confirmed.data["summary"]["applied"], 1)
            self.assertFalse(old_archive.exists())
            with db.connect(database) as connection:
                run = connection.execute(
                    "SELECT status FROM runs WHERE name = 'jmcomic.library.reconcile'"
                ).fetchone()
            self.assertEqual(run["status"], "success")

    def test_rebuilds_wrong_chapter_from_existing_pages_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, library, old_archive, resolved = _seed_wrong_chapter(root)
            quarantine = library / ".trash" / "reconcile-test"

            manifest = jmcomic_reconcile.build_manifest(
                db_path=database,
                library_root=library,
                quarantine_dir=quarantine,
                resolved_by_album={"1215913": [resolved]},
                failed_albums={},
                include_platform_layer=True,
            )
            self.assertEqual(manifest["summary"]["archives_to_rebuild"], 1)
            self.assertEqual(manifest["summary"]["blocked"], 0)
            action = manifest["actions"][0]
            self.assertEqual(action["current_chapter_number"], 1)
            self.assertEqual(action["desired_chapter_number"], 74)
            self.assertNotEqual(action["current_path"], action["desired_path"])

            applied = jmcomic_reconcile.apply_manifest(
                db_path=database,
                library_root=library,
                manifest=manifest,
            )
            self.assertEqual(applied["summary"]["applied"], 1)
            self.assertEqual(applied["summary"]["apply_failed"], 0)
            self.assertFalse(old_archive.exists())
            quarantine_path = Path(applied["apply_results"][0]["quarantine_path"])
            self.assertTrue(quarantine_path.is_file())
            rebuilt = Path(action["desired_path"])
            self.assertTrue(rebuilt.is_file())
            with ZipFile(rebuilt) as archive:
                self.assertEqual(archive.namelist(), ["001.png", "ComicInfo.xml"])
                comic_info = archive.read("ComicInfo.xml").decode("utf-8")
            self.assertIn("<Number>74</Number>", comic_info)

            stored = jmcomic_reconcile.select_jmcomic_items(database)[0]
            self.assertEqual(stored["metadata"]["comic"]["chapter_number"], 74)
            cbz = next(record for record in stored["files"] if record["mime_type"] == CBZ_MIME_TYPE)
            self.assertEqual(Path(cbz["local_path"]), rebuilt)

            rerun = jmcomic_reconcile.build_manifest(
                db_path=database,
                library_root=library,
                quarantine_dir=library / ".trash" / "reconcile-rerun",
                resolved_by_album={"1215913": [resolved]},
                failed_albums={},
                include_platform_layer=True,
            )
            self.assertEqual(rerun["summary"]["already_correct"], 1)
            self.assertEqual(rerun["summary"]["archives_to_rebuild"], 0)

            # A growing album changes Count metadata, but that does not alter
            # Kavita's chapter identity and must not rewrite every old CBZ.
            resolved["metadata"]["comic"]["total_count"] = 75
            count_only = jmcomic_reconcile.build_manifest(
                db_path=database,
                library_root=library,
                quarantine_dir=library / ".trash" / "reconcile-count-only",
                resolved_by_album={"1215913": [resolved]},
                failed_albums={},
                include_platform_layer=True,
            )
            self.assertEqual(count_only["summary"]["metadata_updates"], 1)
            self.assertEqual(count_only["summary"]["archives_to_rebuild"], 0)

    def test_archive_already_in_trash_is_not_moved_or_restored(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, library, old_archive, resolved = _seed_wrong_chapter(root)
            trashed = library / ".trash" / "operator-cleanup" / old_archive.name
            trashed.parent.mkdir(parents=True)
            old_archive.replace(trashed)
            with db.connect(database) as connection:
                connection.execute(
                    "UPDATE media_files SET local_path = ?, library_relative_path = ? WHERE file_key = 'archive:cbz'",
                    (str(trashed), trashed.relative_to(library).as_posix()),
                )

            manifest = jmcomic_reconcile.build_manifest(
                db_path=database,
                library_root=library,
                quarantine_dir=library / ".trash" / "reconcile-test",
                resolved_by_album={"1215913": [resolved]},
                failed_albums={},
                include_platform_layer=True,
            )
            action = manifest["actions"][0]
            self.assertTrue(action["archive_in_trash"])
            self.assertIsNone(action["current_path"])
            self.assertIsNone(action["quarantine_path"])

            jmcomic_reconcile.apply_manifest(
                db_path=database,
                library_root=library,
                manifest=manifest,
            )
            self.assertTrue(trashed.is_file())
            self.assertTrue(Path(action["desired_path"]).is_file())

    def test_unresolved_album_blocks_apply_plan_without_mutation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, library, old_archive, _ = _seed_wrong_chapter(root)
            manifest = jmcomic_reconcile.build_manifest(
                db_path=database,
                library_root=library,
                quarantine_dir=library / ".trash" / "reconcile-test",
                resolved_by_album={},
                failed_albums={
                    "1215913": {"code": "jmcomic_request_failed", "exception_type": "JMComicClientError"}
                },
                include_platform_layer=True,
            )
            self.assertEqual(manifest["summary"]["blocked"], 1)
            self.assertEqual(manifest["actions"], [])
            self.assertTrue(old_archive.is_file())


def _seed_wrong_chapter(root: Path) -> tuple[Path, Path, Path, dict]:
    database = root / "state.sqlite3"
    library = root / "library"
    library.mkdir()
    db.initialize_database(database)
    old_item = _jm_item(chapter_number=1, chapter_source="photo_fallback")
    db.upsert_media_item(database, old_item)
    page = library / "jmcomic" / "comic-pages" / "photo-1463677__p0001.png"
    page.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(page)
    page_info = old_item["metadata"]["files"][0]
    db.upsert_media_file(
        database,
        platform="jmcomic",
        remote_id="photo:1463677",
        file_key="page:000001",
        remote_url=page_info["url"],
        local_path=str(page),
        mime_type="image/png",
        size_bytes=page.stat().st_size,
        checksum=None,
        status="downloaded",
        library_relative_path=page.relative_to(library).as_posix(),
        storage_layout="source-v1",
        file_health="valid",
    )
    db.update_media_item_status(
        database,
        platform="jmcomic",
        remote_id="photo:1463677",
        status="downloaded",
    )
    old_relative = comic_archive_relative_path(item=old_item, include_platform_layer=True)
    old_archive = library / old_relative
    package = build_cbz_atomic(
        target_path=old_archive,
        pages=[page],
        item=old_item,
        allowed_root=library,
    )
    db.upsert_media_file(
        database,
        platform="jmcomic",
        remote_id="photo:1463677",
        file_key="archive:cbz",
        remote_url=None,
        local_path=str(old_archive),
        mime_type=CBZ_MIME_TYPE,
        size_bytes=package["size_bytes"],
        checksum=package["checksum"],
        status="downloaded",
        library_relative_path=old_relative.as_posix(),
        storage_layout="comic-kavita-v2",
        file_health="valid",
    )
    resolved = _jm_item(chapter_number=74, chapter_source="album_provider_sort")
    resolved["metadata"]["comic"].update(
        {"provider_chapter_number": 74, "album_position": 74, "chapter_collision_index": 0}
    )
    resolved["metadata"]["jmcomic"].update(
        {
            "provider_chapter_number": 74,
            "album_position": 74,
            "chapter_collision_index": 0,
        }
    )
    return database, library, old_archive, resolved


def _jm_item(*, chapter_number: int | str, chapter_source: str) -> dict:
    remote_url = "https://cdn.example/media/photos/1463677/00001.png"
    return {
        "platform": "jmcomic",
        "remote_id": "photo:1463677",
        "source_url": "https://18comic.vip/photo/1463677/",
        "media_type": "photo",
        "status": "discovered",
        "source_availability": "available",
        "metadata": {
            "title": "飛機杯女神連線中 CH74",
            "work_type": "comic",
            "storage_category": "comic-pages",
            "page_count": 1,
            "comic": {
                "provider": "jmcomic",
                "provider_work_id": "photo:1463677",
                "title": "飛機杯女神連線中 CH74",
                "series_id": "1215913",
                "series_title": "飛機杯女神連線中",
                "directory_title": "JM 1215913",
                "archive_title": f"Chapter {chapter_number}",
                "chapter_number": chapter_number,
                "chapter_number_source": chapter_source,
                "provider_chapter_number": chapter_number,
                "total_count": 74,
                "is_one_shot": False,
            },
            "files": [
                {
                    "url": remote_url,
                    "kind": "image",
                    "page": 0,
                    "storage_category": "comic-pages",
                }
            ],
            "jmcomic": {
                "entity_type": "photo",
                "photo_id": "1463677",
                "album_id": "1215913",
                "chapter_number": chapter_number,
                "chapter_number_source": chapter_source,
                "provider_chapter_number": chapter_number,
            },
        },
    }


if __name__ == "__main__":
    unittest.main()
