import asyncio
import hashlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from mediagent.core import db, library_content
from mediagent.core.tooling import ToolContext, ToolValidationError
from mediagent.tools.defaults import create_default_registry


_AUTO_CHECKSUM = object()


class LibraryContentToolTests(unittest.TestCase):
    def test_schema_v10_is_idempotent_and_migrates_library_content_tables(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            with db.connect(db_path) as connection:
                connection.execute("UPDATE schema_meta SET value = '9' WHERE key = 'schema_version'")
                connection.execute("DROP TABLE library_operations")
                connection.execute("DROP TABLE library_entries")
                connection.execute("DROP TABLE content_blobs")

            db.initialize_database(db_path)
            db.initialize_database(db_path)

            with db.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                media_file_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(media_files)").fetchall()
                }
            self.assertEqual(db.get_schema_version(db_path), "10")
            self.assertTrue({"content_blobs", "library_entries", "library_operations"} <= tables)
            self.assertIn("library_entry_id", media_file_columns)

    def test_general_media_duplicate_collapses_to_one_visible_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            first = root / "pixiv/photo/first.jpg"
            second = root / "telegram/photo/second.jpg"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"same-image")
            second.write_bytes(b"same-image")

            first_record = self._record(db_path, first, platform="pixiv", remote_id="one")
            second_record = self._record(db_path, second, platform="telegram", remote_id="two")
            first_result = library_content.adopt_media_file(db_path, file_id=first_record["id"])
            second_result = library_content.adopt_media_file(db_path, file_id=second_record["id"])
            records = db.list_media_files(db_path, status="downloaded")

            self.assertTrue(first_result["adopted"])
            self.assertTrue(second_result["deduplicated"])
            self.assertTrue(first.is_file())
            self.assertFalse(second.exists())
            self.assertEqual({record["local_path"] for record in records}, {str(first.resolve())})
            self.assertEqual(len({record["library_entry_id"] for record in records}), 1)
            with self.assertRaisesRegex(ValueError, "globally shared content"):
                library_content.ensure_target_write_safe(db_path, first)
            self.assertEqual(first.read_bytes(), b"same-image")
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM library_entries").fetchone()[0], 1)

    def test_comic_contexts_remain_separate_but_share_inode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            first = root / "comic-pages/jmcomic/a/ch1/001.jpg"
            second = root / "comic-pages/jmcomic/b/ch9/001.jpg"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"same-comic-page")
            second.write_bytes(b"same-comic-page")

            first_record = self._record(db_path, first, platform="jmcomic", remote_id="photo:a")
            second_record = self._record(db_path, second, platform="jmcomic", remote_id="photo:b")
            library_content.adopt_media_file(db_path, file_id=first_record["id"])
            second_result = library_content.adopt_media_file(db_path, file_id=second_record["id"])
            records = db.list_media_files(db_path, status="downloaded")
            repeated_plan = library_content.scan_plan(db_path)

            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            self.assertTrue(second_result["hardlinked"])
            self.assertTrue(os.path.samefile(first, second))
            self.assertEqual(len({record["library_entry_id"] for record in records}), 2)
            self.assertEqual(repeated_plan["summary"]["hardlink_candidates"], 0)
            self.assertIn("hardlink_existing", {action["action"] for action in repeated_plan["actions"]})
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0], 1)

    def test_general_projection_hardlinks_when_comic_context_was_seen_first(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            comic = root / "comic-pages/jmcomic/a/001.jpg"
            general = root / "pixiv/photo/general.jpg"
            comic.parent.mkdir(parents=True)
            general.parent.mkdir(parents=True)
            comic.write_bytes(b"cross-context")
            general.write_bytes(b"cross-context")
            comic_record = self._record(db_path, comic, platform="jmcomic", remote_id="photo:a")
            general_record = self._record(db_path, general, platform="pixiv", remote_id="general")

            library_content.adopt_media_file(db_path, file_id=comic_record["id"])
            general_result = library_content.adopt_media_file(db_path, file_id=general_record["id"])

            self.assertTrue(general_result["hardlinked"])
            self.assertTrue(comic.is_file())
            self.assertTrue(general.is_file())
            self.assertTrue(os.path.samefile(comic, general))
            self.assertEqual(
                len({record["library_entry_id"] for record in db.list_media_files(db_path)}),
                2,
            )

    def test_global_scan_dry_run_then_apply_is_idempotent(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            first = root / "photo/a.jpg"
            second = root / "photo/b.jpg"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"legacy-duplicate")
            second.write_bytes(b"legacy-duplicate")
            self._record(db_path, first, platform="pixiv", remote_id="legacy-a", checksum=None)
            self._record(db_path, second, platform="telegram", remote_id="legacy-b", checksum=None)
            dry_context = self._context(temp_dir, root, db_path, dry_run=True)

            dry_result = asyncio.run(registry.run("library.content.deduplicate", {}, dry_context))

            self.assertTrue(dry_result.is_success)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(dry_result.data["plan"]["summary"]["duplicate_paths"], 1)
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0], 0)

            context = self._context(temp_dir, root, db_path)
            applied = asyncio.run(registry.run("library.content.deduplicate", {}, context))
            repeated = asyncio.run(registry.run("library.content.deduplicate", {}, context))
            records = db.list_media_files(db_path, status="downloaded")

            self.assertTrue(applied.is_success)
            self.assertEqual(applied.data["plan"]["applied"]["paths_collapsed"], 1)
            self.assertEqual(sum(path.exists() for path in (first, second)), 1)
            self.assertEqual(len({record["local_path"] for record in records}), 1)
            self.assertTrue(repeated.is_success)
            self.assertEqual(repeated.data["plan"]["applied"]["paths_collapsed"], 0)

    def test_legacy_trash_reconcile_imports_removed_state_and_is_idempotent(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            original = root / "pixiv/photo/2026/08/legacy.jpg"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"legacy-removed-content")
            record = self._record(db_path, original, platform="pixiv", remote_id="legacy-removed")
            trash = root / ".trash/2026-08-27/pixiv/photo/2026/08/legacy.jpg"
            trash.parent.mkdir(parents=True)
            os.replace(original, trash)

            dry_result = asyncio.run(
                registry.run(
                    "library.trash.reconcile",
                    {},
                    self._context(temp_dir, root, db_path, dry_run=True),
                )
            )

            self.assertTrue(dry_result.is_success)
            self.assertEqual(dry_result.data["plan"]["summary"]["source_rows_importable"], 1)
            self.assertEqual(dry_result.data["plan"]["summary"]["blocked_rows"], 0)
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM library_entries").fetchone()[0], 0)

            context = self._context(temp_dir, root, db_path)
            applied = asyncio.run(registry.run("library.trash.reconcile", {}, context))
            removal_id = applied.data["plan"]["actions"][0]["removal_id"]
            repeated = asyncio.run(registry.run("library.trash.reconcile", {}, context))
            stored = db.list_media_files(db_path, platform="pixiv", remote_id="legacy-removed")[0]

            self.assertTrue(applied.is_success)
            self.assertEqual(applied.data["plan"]["applied"]["removed_entries_imported"], 1)
            self.assertEqual(applied.data["plan"]["applied"]["files_moved"], 0)
            self.assertEqual(stored["library_state"], "removed")
            self.assertEqual(stored["local_path"], str(trash.resolve()))
            self.assertEqual(repeated.data["plan"]["summary"]["missing_rows"], 0)
            self.assertEqual(repeated.data["plan"]["applied"]["removed_entries_imported"], 0)
            post_import_scan = library_content.scan_plan(db_path)
            self.assertEqual(post_import_scan["summary"]["trash_files_skipped"], 1)
            self.assertEqual(post_import_scan["summary"]["missing_files"], 0)

            restored = asyncio.run(
                registry.run("library.entry.restore", {"removal_id": removal_id}, context)
            )
            self.assertTrue(restored.is_success)
            self.assertTrue(original.is_file())
            self.assertFalse(trash.exists())
            self.assertEqual(original.read_bytes(), b"legacy-removed-content")
            self.assertEqual(record["id"], stored["id"])

    def test_legacy_trash_reconcile_selects_latest_verified_duplicate(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            original = root / "pixiv/photo/legacy.jpg"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"matching-content")
            self._record(db_path, original, platform="pixiv", remote_id="legacy-duplicates")
            older = root / ".trash/pixiv-unbookmarked-old/photo/legacy.jpg"
            newer = root / ".trash/pixiv-unbookmarked-new/photo/legacy.jpg"
            mismatch = root / ".trash/pixiv-unbookmarked-wrong/photo/legacy.jpg"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            mismatch.parent.mkdir(parents=True)
            os.replace(original, older)
            newer.write_bytes(b"matching-content")
            mismatch.write_bytes(b"wrongggg-content")
            os.utime(older, ns=(100, 100))
            os.utime(newer, ns=(200, 200))
            os.utime(mismatch, ns=(300, 300))

            plan = library_content.legacy_trash_plan(db_path, library_root=root)
            action = plan["actions"][0]

            self.assertEqual(plan["summary"]["blocked_rows"], 0)
            self.assertEqual(action["trash_path"], str(newer.resolve()))
            self.assertEqual(action["duplicate_candidate_paths"], [str(older.resolve())])
            self.assertTrue(older.is_file())
            self.assertTrue(mismatch.is_file())

            result = asyncio.run(
                registry.run(
                    "library.trash.reconcile",
                    {},
                    self._context(temp_dir, root, db_path),
                )
            )
            self.assertTrue(result.is_success)
            self.assertTrue(older.is_file())
            self.assertTrue(newer.is_file())
            self.assertTrue(mismatch.is_file())

    def test_legacy_trash_reconcile_blocks_active_global_identity(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            removed_source = root / "pixiv/photo/removed.jpg"
            active_source = root / "telegram/photo/active.jpg"
            removed_source.parent.mkdir(parents=True)
            active_source.parent.mkdir(parents=True)
            removed_source.write_bytes(b"shared-legacy-content")
            active_source.write_bytes(b"shared-legacy-content")
            removed_record = self._record(
                db_path,
                removed_source,
                platform="pixiv",
                remote_id="legacy-conflict",
            )
            active_record = self._record(
                db_path,
                active_source,
                platform="telegram",
                remote_id="active-conflict",
            )
            library_content.adopt_media_file(db_path, file_id=active_record["id"])
            trash = root / ".trash/2026-08-27/pixiv/photo/removed.jpg"
            trash.parent.mkdir(parents=True)
            os.replace(removed_source, trash)

            dry_result = asyncio.run(
                registry.run(
                    "library.trash.reconcile",
                    {},
                    self._context(temp_dir, root, db_path, dry_run=True),
                )
            )
            applied = asyncio.run(
                registry.run(
                    "library.trash.reconcile",
                    {},
                    self._context(temp_dir, root, db_path),
                )
            )

            self.assertTrue(dry_result.is_success)
            self.assertEqual(dry_result.data["plan"]["summary"]["blocked_rows"], 1)
            self.assertEqual(dry_result.data["plan"]["blocked"][0]["reason"], "active_identity_conflict")
            self.assertFalse(applied.is_success)
            self.assertEqual(applied.error.code, "legacy_trash_reconcile_blocked")
            stored = db.list_media_files(db_path, platform="pixiv", remote_id="legacy-conflict")[0]
            self.assertIsNone(stored["library_entry_id"])
            self.assertEqual(stored["id"], removed_record["id"])

    def test_legacy_trash_reconcile_attaches_to_existing_removed_identity(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            canonical = root / "pixiv/photo/canonical.jpg"
            legacy = root / "telegram/photo/legacy.jpg"
            canonical.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            canonical.write_bytes(b"already-removed-content")
            legacy.write_bytes(b"already-removed-content")
            canonical_record = self._record(db_path, canonical, platform="pixiv", remote_id="canonical")
            legacy_record = self._record(db_path, legacy, platform="telegram", remote_id="legacy")
            canonical_entry = library_content.adopt_media_file(db_path, file_id=canonical_record["id"])
            context = self._context(temp_dir, root, db_path)
            removed = asyncio.run(
                registry.run("library.entry.remove", {"entry_id": canonical_entry["entry_id"]}, context)
            )
            legacy_trash = root / ".trash/2026-08-27/telegram/photo/legacy.jpg"
            legacy_trash.parent.mkdir(parents=True)
            os.replace(legacy, legacy_trash)

            plan = library_content.legacy_trash_plan(db_path, library_root=root)

            self.assertEqual(plan["summary"]["blocked_rows"], 0)
            self.assertEqual(plan["actions"][0]["action"], "attach_removed")
            self.assertEqual(plan["actions"][0]["trash_path"], removed.data["trash_path"])
            applied = library_content.apply_legacy_trash_plan(db_path, plan)
            stored = db.list_media_files(db_path, platform="telegram", remote_id="legacy")[0]
            self.assertEqual(applied["applied"]["removed_entries_imported"], 0)
            self.assertEqual(stored["library_entry_id"], canonical_entry["entry_id"])
            self.assertEqual(stored["library_state"], "removed")
            self.assertTrue(legacy_trash.is_file())
            self.assertEqual(stored["id"], legacy_record["id"])

    def test_remove_suppresses_redownload_and_restore_is_idempotent(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            original = root / "photo/wanted.jpg"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"managed-content")
            record = self._record(db_path, original, platform="pixiv", remote_id="remove-me")
            adoption = library_content.adopt_media_file(db_path, file_id=record["id"])
            context = self._context(temp_dir, root, db_path)

            removed = asyncio.run(
                registry.run(
                    "library.entry.remove",
                    {
                        "path": str(original),
                        "reason": "not wanted",
                        "external_ref": "external-cleanup:42",
                    },
                    context,
                )
            )
            repeated_remove = asyncio.run(
                registry.run("library.entry.remove", {"entry_id": adoption["entry_id"]}, context)
            )
            trash = Path(removed.data["trash_path"])

            self.assertTrue(removed.is_success)
            self.assertFalse(original.exists())
            self.assertTrue(trash.is_file())
            self.assertEqual(repeated_remove.data["result"], "already_removed")
            with self.assertRaisesRegex(ValueError, "explicitly removed"):
                library_content.ensure_target_write_safe(db_path, original)

            redownload = root / "telegram/duplicate.jpg"
            redownload.parent.mkdir(parents=True)
            redownload.write_bytes(b"managed-content")
            redownload_record = self._record(
                db_path,
                redownload,
                platform="telegram",
                remote_id="same-content",
            )
            suppressed = library_content.adopt_media_file(db_path, file_id=redownload_record["id"])
            self.assertEqual(suppressed["state"], "removed")
            self.assertFalse(redownload.exists())
            self.assertEqual(suppressed["target_path"], str(trash.resolve()))

            restored = asyncio.run(
                registry.run(
                    "library.entry.restore",
                    {"removal_id": removed.data["removal_id"]},
                    context,
                )
            )
            repeated_restore = asyncio.run(
                registry.run("library.entry.restore", {"entry_id": adoption["entry_id"]}, context)
            )
            self.assertTrue(restored.is_success)
            self.assertTrue(original.is_file())
            self.assertFalse(trash.exists())
            self.assertEqual(repeated_restore.data["result"], "already_active")

    def test_restore_refuses_different_content_at_original_path(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            original = root / "photo/conflict.jpg"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"original")
            record = self._record(db_path, original, platform="pixiv", remote_id="conflict")
            library_content.adopt_media_file(db_path, file_id=record["id"])
            context = self._context(temp_dir, root, db_path)
            removed = asyncio.run(
                registry.run("library.entry.remove", {"path": str(original)}, context)
            )
            original.write_bytes(b"someone-else")

            restored = asyncio.run(
                registry.run(
                    "library.entry.restore",
                    {"removal_id": removed.data["removal_id"]},
                    context,
                )
            )

            self.assertFalse(restored.is_success)
            self.assertEqual(restored.error.code, "restore_checksum_conflict")
            self.assertEqual(original.read_bytes(), b"someone-else")
            self.assertTrue(Path(removed.data["trash_path"]).is_file())

    def test_remove_recovers_a_planned_operation_after_file_move(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            source = root / "photo/interrupted.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"interrupted-remove")
            record = self._record(db_path, source, platform="pixiv", remote_id="interrupted-remove")
            adoption = library_content.adopt_media_file(db_path, file_id=record["id"])
            target = root / ".trash/mediagent/rmv_interrupted/photo/interrupted.jpg"
            target.parent.mkdir(parents=True)
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO library_operations (
                        id, operation_type, library_entry_id, state, original_path,
                        target_path, metadata_json, created_at
                    ) VALUES ('rmv_interrupted', 'remove', ?, 'planned', ?, ?, '{}', 'now')
                    """,
                    (adoption["entry_id"], str(source), str(target)),
                )
            os.replace(source, target)

            result = asyncio.run(
                registry.run(
                    "library.entry.remove",
                    {"entry_id": adoption["entry_id"]},
                    self._context(temp_dir, root, db_path),
                )
            )

            self.assertTrue(result.is_success)
            self.assertEqual(result.data["result"], "recovered_removed")
            self.assertEqual(result.data["removal_id"], "rmv_interrupted")
            self.assertTrue(target.is_file())
            self.assertEqual(result.data["entry"]["state"], "removed")

    def test_rename_recovers_a_planned_operation_after_file_move(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            source = root / "photo/interrupted.jpg"
            target = root / "photo/recovered.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"interrupted-rename")
            record = self._record(db_path, source, platform="pixiv", remote_id="interrupted-rename")
            adoption = library_content.adopt_media_file(db_path, file_id=record["id"])
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO library_operations (
                        id, operation_type, library_entry_id, state, original_path,
                        target_path, metadata_json, created_at
                    ) VALUES ('ren_interrupted', 'rename', ?, 'planned', ?, ?, ?, 'now')
                    """,
                    (
                        adoption["entry_id"],
                        str(source),
                        str(target),
                        '{"display_name":"recovered"}',
                    ),
                )
            os.replace(source, target)

            result = asyncio.run(
                registry.run(
                    "library.entry.rename",
                    {"entry_id": adoption["entry_id"], "name": "recovered"},
                    self._context(temp_dir, root, db_path),
                )
            )

            self.assertTrue(result.is_success)
            self.assertTrue(result.data["recovered"])
            self.assertEqual(result.data["rename_id"], "ren_interrupted")
            self.assertTrue(target.is_file())

    def test_rename_updates_shared_source_rows_and_audit_reference(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            first = root / "photo/source.jpg"
            duplicate = root / "telegram/copy.jpg"
            first.parent.mkdir(parents=True)
            duplicate.parent.mkdir(parents=True)
            first.write_bytes(b"rename-content")
            duplicate.write_bytes(b"rename-content")
            one = self._record(db_path, first, platform="pixiv", remote_id="rename-a")
            two = self._record(db_path, duplicate, platform="telegram", remote_id="rename-b")
            adoption = library_content.adopt_media_file(db_path, file_id=one["id"])
            library_content.adopt_media_file(db_path, file_id=two["id"])
            context = self._context(temp_dir, root, db_path)

            renamed = asyncio.run(
                registry.run(
                    "library.entry.rename",
                    {
                        "entry_id": adoption["entry_id"],
                        "name": "new title",
                        "external_ref": "external-catalog:99",
                    },
                    context,
                )
            )
            new_path = first.with_name("new title.jpg")
            records = db.list_media_files(db_path, status="downloaded")

            self.assertTrue(renamed.is_success)
            self.assertFalse(first.exists())
            self.assertTrue(new_path.is_file())
            self.assertEqual({record["local_path"] for record in records}, {str(new_path.resolve())})
            self.assertEqual({record["display_name_override"] for record in records}, {"new title"})
            with db.connect(db_path) as connection:
                operation = connection.execute(
                    "SELECT external_ref, state FROM library_operations WHERE id = ?",
                    (renamed.data["rename_id"],),
                ).fetchone()
            self.assertEqual(
                dict(operation),
                {"external_ref": "external-catalog:99", "state": "completed"},
            )

    def test_renamed_single_source_keeps_name_when_content_revision_arrives(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            source = root / "photo/source.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"revision-one")
            record = self._record(db_path, source, platform="pixiv", remote_id="revision")
            adoption = library_content.adopt_media_file(db_path, file_id=record["id"])
            renamed = asyncio.run(
                registry.run(
                    "library.entry.rename",
                    {"entry_id": adoption["entry_id"], "name": "persistent name"},
                    self._context(temp_dir, root, db_path),
                )
            )
            renamed_path = Path(renamed.data["new_path"])
            source.write_bytes(b"revision-two")
            new_checksum, new_size = library_content.sha256_checksum(source)
            revised_record = db.upsert_media_file(
                db_path,
                platform="pixiv",
                remote_id="revision",
                remote_url="https://example.invalid/pixiv/revision",
                local_path=str(source),
                mime_type="image/jpeg",
                size_bytes=new_size,
                checksum=new_checksum,
                status="downloaded",
                library_relative_path="photo/source.jpg",
                file_key="source:0",
            )
            revised = library_content.adopt_media_file(db_path, file_id=revised_record["id"])

            self.assertTrue(revised["adopted"])
            self.assertFalse(source.exists())
            self.assertEqual(renamed_path.read_bytes(), b"revision-two")
            self.assertEqual(revised["target_path"], str(renamed_path))
            self.assertEqual(revised["entry_id"], adoption["entry_id"])

    def test_cbz_rename_rewrites_comicinfo_title(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            source = root / "comic/jmcomic/old.cbz"
            source.parent.mkdir(parents=True)
            with ZipFile(source, "w") as archive:
                archive.writestr("ComicInfo.xml", "<?xml version='1.0'?><ComicInfo><Title>Old</Title></ComicInfo>")
                archive.writestr("001.jpg", b"page")
            record = self._record(
                db_path,
                source,
                platform="jmcomic",
                remote_id="photo:cbz",
                mime_type="application/vnd.comicbook+zip",
            )
            adoption = library_content.adopt_media_file(db_path, file_id=record["id"])
            old_checksum = record["checksum"]
            context = self._context(temp_dir, root, db_path)

            renamed = asyncio.run(
                registry.run(
                    "library.entry.rename",
                    {"entry_id": adoption["entry_id"], "name": "New Chapter"},
                    context,
                )
            )
            target = source.with_name("New Chapter.cbz")
            with ZipFile(target, "r") as archive:
                comic_info = archive.read("ComicInfo.xml").decode("utf-8")

            self.assertTrue(renamed.is_success)
            self.assertTrue(renamed.data["content_changed"])
            self.assertIn("<Title>New Chapter</Title>", comic_info)
            self.assertNotEqual(renamed.data["entry"]["checksum"], old_checksum)

    def test_remove_restore_rename_do_not_support_dry_run(self) -> None:
        registry = create_default_registry()
        for name in ("library.entry.remove", "library.entry.restore", "library.entry.rename"):
            self.assertFalse(registry.inspect(name).dry_run_supported)
            with self.assertRaises(ToolValidationError) as raised:
                asyncio.run(
                    registry.run(
                        name,
                        {"entry_id": "missing", **({"name": "new"} if name.endswith("rename") else {})},
                        ToolContext.from_env(env={}, dry_run=True),
                    )
                )
            self.assertEqual(raised.exception.error.code, "dry_run_not_supported")

    def test_media_file_upsert_rejects_unsafe_path_before_db_mutation(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            db.upsert_media_item(
                db_path,
                {"platform": "pixiv", "remote_id": "unsafe-file", "media_type": "photo"},
            )
            outside = Path(temp_dir) / "outside/file.jpg"
            outside.parent.mkdir(parents=True)
            outside.write_bytes(b"outside")
            checksum, size_bytes = library_content.sha256_checksum(outside)

            result = asyncio.run(
                registry.run(
                    "media.file.upsert",
                    {
                        "platform": "pixiv",
                        "remote_id": "unsafe-file",
                        "local_path": str(outside),
                        "mime_type": "image/jpeg",
                        "size_bytes": size_bytes,
                        "checksum": checksum,
                        "status": "downloaded",
                    },
                    self._context(temp_dir, root, db_path),
                )
            )
            records = db.list_media_files(db_path, platform="pixiv", remote_id="unsafe-file")

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "unsafe_path")
            self.assertEqual(records, [])

    def test_rename_rejects_path_components(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            source = root / "photo/safe.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"safe")
            record = self._record(db_path, source, platform="pixiv", remote_id="safe")
            adoption = library_content.adopt_media_file(db_path, file_id=record["id"])

            result = asyncio.run(
                registry.run(
                    "library.entry.rename",
                    {"entry_id": adoption["entry_id"], "name": "../escape"},
                    self._context(temp_dir, root, db_path),
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "library_rename_failed")
            self.assertTrue(source.is_file())

    def test_rename_rejects_path_reserved_by_removed_entry(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            first = root / "photo/first.jpg"
            reserved = root / "photo/reserved.jpg"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"first-content")
            reserved.write_bytes(b"reserved-content")
            first_record = self._record(db_path, first, platform="pixiv", remote_id="first")
            reserved_record = self._record(db_path, reserved, platform="pixiv", remote_id="reserved")
            first_entry = library_content.adopt_media_file(db_path, file_id=first_record["id"])
            reserved_entry = library_content.adopt_media_file(db_path, file_id=reserved_record["id"])
            context = self._context(temp_dir, root, db_path)
            asyncio.run(
                registry.run(
                    "library.entry.remove",
                    {"entry_id": reserved_entry["entry_id"]},
                    context,
                )
            )

            result = asyncio.run(
                registry.run(
                    "library.entry.rename",
                    {"entry_id": first_entry["entry_id"], "name": "reserved"},
                    context,
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "rename_path_conflict")
            self.assertTrue(first.is_file())
            self.assertFalse(reserved.exists())

    def test_managed_trash_prepare_reports_namespace_and_retention(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            root, db_path = self._workspace(temp_dir)
            context = self._context(temp_dir, root, db_path)

            before = asyncio.run(registry.run("library.trash.status", {}, context))
            prepared = asyncio.run(registry.run("library.trash.prepare", {}, context))
            after = asyncio.run(registry.run("library.trash.status", {}, context))

            self.assertTrue(before.is_success)
            self.assertFalse(before.data["operational"])
            self.assertTrue(prepared.is_success)
            self.assertTrue(prepared.data["operational"])
            self.assertEqual(Path(prepared.data["managed_path"]), root / ".trash/mediagent")
            self.assertFalse(prepared.data["retention"]["automatic_purge"])
            self.assertTrue(after.data["managed"]["writable"])

    def test_managed_trash_prepare_rejects_symlinked_trash_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _ = self._workspace(temp_dir)
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            (root / ".trash").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                library_content.prepare_managed_trash(root)

            status = library_content.managed_trash_status(root)
            self.assertFalse(status["operational"])
            self.assertTrue(status["trash"]["is_symlink"])

    @staticmethod
    def _workspace(temp_dir: str) -> tuple[Path, Path]:
        root = Path(temp_dir) / "library"
        db_path = Path(temp_dir) / "data/mediagent.sqlite3"
        root.mkdir(parents=True)
        db.initialize_database(db_path)
        return root, db_path

    @staticmethod
    def _context(
        temp_dir: str,
        root: Path,
        db_path: Path,
        *,
        dry_run: bool = False,
    ) -> ToolContext:
        return ToolContext.from_env(
            env={
                "MEDIAGENT_DATA_DIR": str(Path(temp_dir) / "data"),
                "MEDIAGENT_LIBRARY_DIR": str(root),
                "MEDIAGENT_DB_PATH": str(db_path),
            },
            cwd=Path(temp_dir),
            dry_run=dry_run,
        )

    @staticmethod
    def _record(
        db_path: Path,
        path: Path,
        *,
        platform: str,
        remote_id: str,
        checksum: str | None | object = _AUTO_CHECKSUM,
        mime_type: str = "image/jpeg",
    ) -> dict:
        db.upsert_media_item(
            db_path,
            {"platform": platform, "remote_id": remote_id, "media_type": "photo"},
        )
        actual_checksum = (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if checksum is _AUTO_CHECKSUM
            else checksum
        )
        library_root = next(parent for parent in path.parents if parent.name == "library")
        return db.upsert_media_file(
            db_path,
            platform=platform,
            remote_id=remote_id,
            remote_url=f"https://example.invalid/{platform}/{remote_id}",
            local_path=str(path.resolve()),
            mime_type=mime_type,
            size_bytes=path.stat().st_size,
            checksum=actual_checksum,
            status="downloaded",
            library_relative_path=path.relative_to(library_root).as_posix(),
            file_key="source:0",
        )


if __name__ == "__main__":
    unittest.main()
