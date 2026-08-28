import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from mediagent.core import db
from mediagent.core.comics import comic_descriptor
from mediagent.core.tooling import ToolContext
from mediagent.platforms.pixiv import parser as pixiv_parser
from mediagent.tools.defaults import create_default_registry


PLACEHOLDER_URL = "https://s.pximg.net/common/images/limit_unknown_360.png"


class PixivLibraryToolTests(unittest.TestCase):
    def test_comic_descriptor_preserves_identity_with_long_unicode_title(self) -> None:
        descriptor = comic_descriptor(
            {
                "platform": "pixiv",
                "remote_id": "long-1",
                "metadata": {"title": "漫" * 200},
            }
        )

        self.assertLessEqual(len(descriptor.series_directory.encode("utf-8")), 180)
        self.assertLessEqual(len(descriptor.archive_filename.encode("utf-8")), 224)
        self.assertTrue(descriptor.series_directory.endswith("[pixiv-long-1]"))
        self.assertTrue(descriptor.archive_filename.endswith("[pixiv-long-1].cbz"))

    def test_parser_marks_invisible_and_placeholder_artworks_unavailable(self) -> None:
        invisible = _illust("invisible", pixiv_type="illust", visible=False, url=PLACEHOLDER_URL)
        placeholder = _illust("placeholder", pixiv_type="illust", visible=None, url=PLACEHOLDER_URL)

        invisible_item = pixiv_parser.parse_illust(invisible)
        placeholder_item = pixiv_parser.parse_illust(placeholder)

        self.assertEqual(invisible_item["source_availability"], "unavailable")
        self.assertEqual(invisible_item["status"], "skipped")
        self.assertEqual(invisible_item["metadata"]["availability_reason"], "visible_false")
        self.assertEqual(invisible_item["metadata"]["files"], [])
        self.assertEqual(placeholder_item["metadata"]["availability_reason"], "placeholder_asset")
        self.assertEqual(placeholder_item["metadata"]["files"], [])

    def test_reconcile_plan_does_not_move_or_update(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            comic_path = _seed_file(
                db_path,
                data_dir,
                remote_id="comic-1",
                pixiv_type="manga",
                relative_path="pixiv/photo/2026/01/comic-1_p0.jpg",
                remote_url="https://i.pximg.net/comic-1_p0.jpg",
            )

            result = asyncio.run(registry.run("pixiv.library.reconcile", {"mode": "plan"}, context))

            self.assertTrue(result.is_success)
            self.assertTrue(comic_path.exists())
            self.assertFalse((data_dir / "library" / "pixiv" / "comic-pages" / "2026" / "01" / comic_path.name).exists())
            self.assertEqual(result.data["manifest"]["summary"]["files_to_move"], 1)
            with db.connect(db_path) as connection:
                metadata = json.loads(connection.execute("SELECT metadata_json FROM media_items").fetchone()[0])
            self.assertNotIn("work_type", metadata)

    def test_reconcile_apply_moves_comics_and_quarantines_placeholders(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            comic_path = _seed_file(
                db_path,
                data_dir,
                remote_id="comic-1",
                pixiv_type="manga",
                relative_path="pixiv/photo/2026/01/comic-1_p0.jpg",
                remote_url="https://i.pximg.net/comic-1_p0.jpg",
            )
            comic_sidecar = comic_path.with_suffix(".json")
            comic_sidecar.write_text('{"sample":true}\n', encoding="utf-8")
            placeholder_path = _seed_file(
                db_path,
                data_dir,
                remote_id="gone-1",
                pixiv_type="illust",
                relative_path="pixiv/photo/2026/01/gone-1_p0.png",
                remote_url=PLACEHOLDER_URL,
                visible=False,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.library.reconcile",
                    {"mode": "apply", "confirm": True, "include_details": True},
                    context,
                )
            )

            comic_target = data_dir / "library" / "pixiv" / "comic-pages" / "2026" / "01" / comic_path.name
            self.assertTrue(result.is_success)
            self.assertFalse(comic_path.exists())
            self.assertTrue(comic_target.exists())
            self.assertTrue(comic_target.with_suffix(".json").exists())
            self.assertFalse(placeholder_path.exists())
            quarantine_files = [path for path in (data_dir / "quarantine" / "pixiv-library").rglob("*") if path.is_file()]
            self.assertEqual(len(quarantine_files), 1)
            with db.connect(db_path) as connection:
                rows = connection.execute(
                    """
                    SELECT mi.remote_id, mi.status, mi.source_availability, mi.metadata_json,
                           mf.local_path, mf.library_relative_path
                    FROM media_items mi
                    LEFT JOIN media_files mf ON mf.media_item_id = mi.id
                    ORDER BY mi.remote_id
                    """
                ).fetchall()
            comic_row, gone_row = rows
            comic_metadata = json.loads(comic_row["metadata_json"])
            gone_metadata = json.loads(gone_row["metadata_json"])
            self.assertEqual(comic_metadata["work_type"], "comic")
            self.assertEqual(comic_metadata["storage_category"], "comic-pages")
            self.assertEqual(comic_metadata["comic"]["provider_work_id"], "comic-1")
            self.assertTrue(comic_metadata["comic"]["is_one_shot"])
            self.assertEqual(comic_row["library_relative_path"], "pixiv/comic-pages/2026/01/comic-1_p0.jpg")
            self.assertEqual(comic_row["local_path"], str(comic_target))
            self.assertEqual(gone_row["status"], "skipped")
            self.assertEqual(gone_row["source_availability"], "unavailable")
            self.assertEqual(gone_metadata["availability_reason"], "visible_false")
            self.assertIsNone(gone_row["local_path"])

    def test_reconcile_apply_requires_confirmation(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            path = _seed_file(
                db_path,
                data_dir,
                remote_id="comic-1",
                pixiv_type="manga",
                relative_path="pixiv/photo/2026/01/comic-1_p0.jpg",
                remote_url="https://i.pximg.net/comic-1_p0.jpg",
            )

            result = asyncio.run(registry.run("pixiv.library.reconcile", {"mode": "apply"}, context))

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "pixiv_reconcile_not_confirmed")
            self.assertTrue(path.exists())

    def test_comics_package_builds_cbz_records_it_and_dedupes_rerun(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            for page in (0, 1):
                _seed_file(
                    db_path,
                    data_dir,
                    remote_id="comic-1",
                    pixiv_type="manga",
                    relative_path=f"pixiv/comic-pages/2026/01/20260101__pixiv__comic-1__p{page}.jpg",
                    remote_url=f"https://i.pximg.net/comic-1_p{page}.jpg",
                )

            dry_run_context = ToolContext.from_env(
                env=context.env,
                cwd=context.cwd,
                dry_run=True,
            )
            planned = asyncio.run(registry.run("pixiv.comics.package", {}, dry_run_context))
            cbz_path = (
                data_dir
                / "library"
                / "pixiv"
                / "comic"
                / "Comic One [pixiv-comic-1]"
                / "Comic One [pixiv-comic-1].cbz"
            )
            self.assertFalse(cbz_path.exists())
            packaged = asyncio.run(registry.run("pixiv.comics.package", {}, context))
            repeated = asyncio.run(registry.run("pixiv.comics.package", {}, context))
            files = db.list_media_files(db_path, platform="pixiv", remote_id="comic-1")

            self.assertTrue(planned.is_success)
            self.assertEqual(planned.data["summary"]["ready"], 1)
            self.assertTrue(packaged.is_success)
            self.assertEqual(packaged.data["summary"]["packaged"], 1)
            self.assertTrue(repeated.is_success)
            self.assertEqual(repeated.data["summary"]["existing"], 1)
            self.assertEqual(len(files), 3)
            cbz_record = next(file for file in files if file["local_path"] == str(cbz_path))
            self.assertEqual(cbz_record["mime_type"], "application/vnd.comicbook+zip")
            self.assertEqual(cbz_record["storage_layout"], "comic-kavita-v2")
            self.assertTrue(str(cbz_record["checksum"]).startswith("sha256:"))
            with ZipFile(cbz_path) as archive:
                self.assertEqual(archive.namelist(), ["001.jpg", "002.jpg", "ComicInfo.xml"])
                comic_info = archive.read("ComicInfo.xml")
                self.assertIn(b"<PageCount>2</PageCount>", comic_info)
                self.assertIn(b"<Series>Comic One [Pixiv comic-1]</Series>", comic_info)
                self.assertIn(b"<Number>1</Number>", comic_info)
                self.assertIn(b"<Count>1</Count>", comic_info)
                self.assertIn(b"<Format>One-Shot</Format>", comic_info)

    def test_comics_package_does_not_fail_for_explicitly_removed_archive(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            _seed_file(
                db_path,
                data_dir,
                remote_id="removed-comic",
                pixiv_type="manga",
                relative_path="pixiv/comic-pages/2026/01/removed-comic_p0.jpg",
                remote_url="https://i.pximg.net/removed-comic_p0.jpg",
            )
            packaged = asyncio.run(registry.run("pixiv.comics.package", {}, context))
            archive = Path(packaged.data["packages"][0]["target_path"])
            removed = asyncio.run(
                registry.run("library.entry.remove", {"path": str(archive)}, context)
            )

            rerun = asyncio.run(registry.run("pixiv.comics.package", {}, context))

            self.assertTrue(removed.is_success)
            self.assertTrue(rerun.is_success)
            self.assertEqual(rerun.data["summary"]["skipped"], 1)
            self.assertEqual(rerun.data["summary"]["failed"], 0)

    def test_comics_package_groups_real_series_in_one_kavita_folder(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            for remote_id, order in (("chapter-1", 1), ("chapter-2", 2)):
                _seed_file(
                    db_path,
                    data_dir,
                    remote_id=remote_id,
                    pixiv_type="manga",
                    title=f"Episode {order}",
                    series={"id": "77", "title": "Sample Series", "order": order, "count": 2},
                    relative_path=f"pixiv/comic-pages/2026/01/{remote_id}__p0.jpg",
                    remote_url=f"https://i.pximg.net/{remote_id}_p0.jpg",
                )

            result = asyncio.run(registry.run("pixiv.comics.package", {}, context))
            series_dir = data_dir / "library" / "pixiv" / "comic" / "Sample Series [pixiv-series-77]"
            archives = sorted(series_dir.glob("*.cbz"))

            self.assertTrue(result.is_success)
            self.assertEqual(result.data["summary"]["packaged"], 2)
            self.assertEqual(
                [path.name for path in archives],
                [
                    "Sample Series - c1 [pixiv-chapter-1].cbz",
                    "Sample Series - c2 [pixiv-chapter-2].cbz",
                ],
            )
            with ZipFile(archives[1]) as archive:
                comic_info = archive.read("ComicInfo.xml")
            self.assertIn(b"<Series>Sample Series [Pixiv series 77]</Series>", comic_info)
            self.assertIn(b"<Number>2</Number>", comic_info)
            self.assertIn(b"<Count>2</Count>", comic_info)

    def test_comics_package_refuses_missing_source_pages(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            source = _seed_file(
                db_path,
                data_dir,
                remote_id="comic-missing",
                pixiv_type="manga",
                relative_path="pixiv/comic-pages/2026/01/20260101__pixiv__comic-missing__p0.jpg",
                remote_url="https://i.pximg.net/comic-missing_p0.jpg",
            )
            source.unlink()

            result = asyncio.run(registry.run("pixiv.comics.package", {}, context))
            cbz_files = list((data_dir / "library").rglob("*.cbz"))

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "pixiv_comic_package_partial")
            self.assertEqual(result.data["summary"]["failed"], 1)
            self.assertEqual(cbz_files, [])

    def test_comics_package_rebuilds_and_retires_legacy_v1_cbz(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            _seed_file(
                db_path,
                data_dir,
                remote_id="legacy-1",
                pixiv_type="manga",
                title="Legacy Comic",
                relative_path="pixiv/comic-pages/2026/01/legacy-1__p0.jpg",
                remote_url="https://i.pximg.net/legacy-1_p0.jpg",
            )
            legacy_path = data_dir / "library" / "pixiv" / "comic" / "2026" / "01" / "legacy.cbz"
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(b"legacy-cbz")
            db.upsert_media_file(
                db_path,
                platform="pixiv",
                remote_id="legacy-1",
                remote_url=None,
                local_path=str(legacy_path),
                mime_type="application/vnd.comicbook+zip",
                size_bytes=legacy_path.stat().st_size,
                checksum=None,
                status="downloaded",
                library_relative_path="pixiv/comic/2026/01/legacy.cbz",
                storage_layout="comic-cbz-v1",
                file_health="valid",
            )
            dry_run_context = ToolContext.from_env(env=context.env, cwd=context.cwd, dry_run=True)

            blocked = asyncio.run(registry.run("pixiv.comics.package", {}, dry_run_context))
            migrated = asyncio.run(
                registry.run("pixiv.comics.package", {"migrate_legacy": True}, context)
            )
            new_path = (
                data_dir
                / "library"
                / "pixiv"
                / "comic"
                / "Legacy Comic [pixiv-legacy-1]"
                / "Legacy Comic [pixiv-legacy-1].cbz"
            )
            quarantined = list((data_dir / "library" / ".trash" / "mediagent").rglob("*.cbz"))
            files = db.list_media_files(db_path, platform="pixiv", remote_id="legacy-1")
            repeated = asyncio.run(registry.run("pixiv.comics.package", {}, context))

            self.assertTrue(blocked.is_success)
            self.assertEqual(blocked.data["summary"]["blocked"], 1)
            self.assertTrue(migrated.is_success)
            self.assertEqual(migrated.data["summary"]["packaged"], 1)
            self.assertEqual(migrated.data["summary"]["legacy_cbz_retired"], 1)
            self.assertEqual(migrated.data["summary"]["legacy_cbz_quarantined"], 1)
            self.assertFalse(legacy_path.exists())
            self.assertTrue(new_path.exists())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(len(files), 3)
            cbz_files = [file for file in files if file["mime_type"] == "application/vnd.comicbook+zip"]
            self.assertEqual(len(cbz_files), 2)
            self.assertEqual(sum(file["library_state"] == "active" for file in cbz_files), 1)
            self.assertEqual(sum(file["library_state"] == "removed" for file in cbz_files), 1)
            removed_cbz = next(file for file in cbz_files if file["library_state"] == "removed")
            self.assertEqual(Path(removed_cbz["local_path"]), quarantined[0])
            self.assertTrue(repeated.is_success)
            self.assertEqual(repeated.data["summary"]["existing"], 1)
            self.assertEqual(repeated.data["summary"]["failed"], 0)


def _context(temp_dir: str) -> tuple[Path, Path, ToolContext]:
    data_dir = Path(temp_dir) / "data"
    db_path = data_dir / "mediagent.sqlite3"
    context = ToolContext.from_env(
        env={
            "MEDIAGENT_DATA_DIR": str(data_dir),
            "MEDIAGENT_DB_PATH": str(db_path),
        },
        cwd=Path(temp_dir),
    )
    return data_dir, db_path, context


def _seed_file(
    db_path: Path,
    data_dir: Path,
    *,
    remote_id: str,
    pixiv_type: str,
    relative_path: str,
    remote_url: str,
    visible: bool = True,
    title: str = "Comic One",
    series: dict[str, object] | None = None,
) -> Path:
    path = data_dir / "library" / Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pixiv-test")
    db.initialize_database(db_path)
    db.upsert_media_item(
        db_path,
        {
            "platform": "pixiv",
            "remote_id": remote_id,
            "media_type": "photo",
            "status": "downloaded",
            "metadata": {
                "pixiv_type": pixiv_type,
                "title": title,
                "visible": visible,
                "create_date": "2026-01-01T00:00:00+00:00",
                "series": series,
            },
        },
    )
    db.upsert_media_file(
        db_path,
        platform="pixiv",
        remote_id=remote_id,
        remote_url=remote_url,
        local_path=str(path),
        mime_type="image/png" if path.suffix == ".png" else "image/jpeg",
        size_bytes=path.stat().st_size,
        checksum=None,
        status="downloaded",
        library_relative_path=relative_path,
        storage_layout="scanner-friendly-v2",
        file_health="valid",
    )
    db.update_media_item_status(db_path, platform="pixiv", remote_id=remote_id, status="downloaded")
    return path


def _illust(
    illust_id: str,
    *,
    pixiv_type: str,
    visible: bool | None,
    url: str,
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": illust_id,
        "type": pixiv_type,
        "page_count": 1,
        "image_urls": {"large": url},
        "meta_single_page": {},
        "meta_pages": [],
        "user": {},
    }
    if visible is not None:
        value["visible"] = visible
    return value
