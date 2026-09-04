from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from mediagent.core import assets, db, lifecycle, library_content
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext
from mediagent.tools import link_tools, pixiv_tools, telegram_tools
from mediagent.tools.defaults import create_default_registry
from mediagent.tools.pixiv_library_tools import _comic_package_plan


class AssetLifecycleTests(unittest.TestCase):
    def test_lifecycle_operations_require_an_existing_database(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            library.mkdir()
            db_path = root / "missing.sqlite3"

            with self.assertRaisesRegex(FileNotFoundError, "Database does not exist"):
                lifecycle.remove_asset(
                    db_path,
                    asset_id="asset_missing",
                    library_root=library,
                )
            with self.assertRaisesRegex(FileNotFoundError, "Database does not exist"):
                lifecycle.restore_asset(
                    db_path,
                    asset_id="asset_missing",
                    library_root=library,
                )
            with self.assertRaisesRegex(FileNotFoundError, "Database does not exist"):
                lifecycle.purge_trash(
                    db_path,
                    library_root=library,
                    retention_days=30,
                    dry_run=True,
                )

            self.assertFalse(db_path.exists())

    def test_lifecycle_tools_report_missing_database_without_creating_it(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            library.mkdir()
            db_path = root / "missing.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root / "data"),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                },
            )
            preview_context = ToolContext.from_env(
                cwd=root,
                env=dict(context.env),
                dry_run=True,
            )
            registry = create_default_registry()

            purge = asyncio.run(registry.run("library.trash.purge", {}, preview_context))
            remove = asyncio.run(
                registry.run("library.asset.remove", {"asset_id": "ast_missing"}, context)
            )
            restore = asyncio.run(
                registry.run("library.asset.restore", {"asset_id": "ast_missing"}, context)
            )

            self.assertEqual(purge.error.code, "missing_db")
            self.assertEqual(remove.error.code, "missing_db")
            self.assertEqual(restore.error.code, "missing_db")
            self.assertFalse(db_path.exists())

    def test_unknown_asset_has_a_validation_error_code(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            library.mkdir()
            db_path = root / "mediagent.sqlite3"
            db.initialize_database(db_path)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                },
            )
            registry = create_default_registry()

            remove = asyncio.run(
                registry.run("library.asset.remove", {"asset_id": "ast_missing"}, context)
            )
            restore = asyncio.run(
                registry.run("library.asset.restore", {"asset_id": "ast_missing"}, context)
            )

            self.assertEqual(remove.error.code, "asset_not_found")
            self.assertEqual(remove.error.category.value, "validation")
            self.assertEqual(restore.error.code, "asset_not_found")
            self.assertEqual(restore.error.category.value, "validation")

    def test_purge_preview_does_not_upgrade_an_old_database(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            library.mkdir()
            db_path = root / "mediagent.sqlite3"
            db.initialize_database(db_path)
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE schema_meta SET value = '12' WHERE key = 'schema_version'"
                )

            with self.assertRaisesRegex(ValueError, "must be upgraded"):
                lifecycle.purge_trash(
                    db_path,
                    library_root=library,
                    retention_days=30,
                    dry_run=True,
                )

            self.assertEqual(db.get_schema_version(db_path), "12")

    def test_remove_and_restore_operate_on_the_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, context, source, asset_id, target = self._imported(temp_dir)

            removed = lifecycle.remove_asset(
                db_path,
                asset_id=asset_id,
                library_root=library,
                reason="not wanted",
            )

            self.assertEqual(removed["result"], "removed")
            self.assertFalse(target.exists())
            trash_path = Path(removed["trash_paths"][0])
            self.assertTrue(trash_path.is_file())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "removed")

            restored = lifecycle.restore_asset(
                db_path,
                asset_id=asset_id,
                library_root=library,
            )

            self.assertEqual(restored["result"], "restored")
            self.assertTrue(target.is_file())
            self.assertFalse(trash_path.exists())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")
            self.assertTrue(source.is_file())

    def test_legacy_entry_restore_normalizes_asset_lifecycle_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, target = self._imported(temp_dir)
            lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            removed_asset = assets.load_asset(db_path, asset_id)
            self.assertIsNotNone(removed_asset["removed_at"])
            with db.connect(db_path) as connection:
                entry_id = connection.execute(
                    "SELECT library_entry_id FROM asset_representations WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()[0]

            restored = library_content.restore_entry(
                db_path,
                entry_id=str(entry_id),
            )

            self.assertTrue(restored["changed"])
            self.assertTrue(target.is_file())
            refreshed = assets.load_asset(db_path, asset_id)
            self.assertEqual(refreshed["state"], "active")
            self.assertIsNone(refreshed["removed_at"])
            self.assertIsNone(refreshed["purged_at"])
            self.assertIsNone(refreshed["purge_reason"])

    def test_purge_deletes_bytes_retains_tombstone_and_blocks_local_readd(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, context, source, asset_id, target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(
                db_path,
                asset_id=asset_id,
                library_root=library,
                reason="not wanted",
            )
            trash_path = Path(removed["trash_paths"][0])
            purge_now = datetime.now(UTC) + timedelta(seconds=1)

            purged = lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=purge_now,
            )

            self.assertEqual(purged["assets_purged"], 1)
            self.assertEqual(purged["paths_unlinked"], 1)
            self.assertFalse(target.exists())
            self.assertFalse(trash_path.exists())
            tombstone = assets.load_asset(db_path, asset_id)
            self.assertEqual(tombstone["state"], "purged")
            self.assertIsNotNone(tombstone["purged_at"])
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset_representations").fetchone()[0], 1)

            blocked = self._run_import(context, source)

            self.assertFalse(blocked.is_success)
            self.assertEqual(blocked.error.code, "local_content_removed")
            self.assertEqual(blocked.data["summary"]["blocked"], 1)
            self.assertEqual(blocked.data["results"][0]["asset_id"], asset_id)
            self.assertEqual(list(library.rglob("*.png")), [])
            with self.assertRaisesRegex(ValueError, "permanently purged"):
                lifecycle.restore_asset(db_path, asset_id=asset_id, library_root=library)

    def test_purge_dry_run_keeps_removed_content(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, _target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])

            result = lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=True,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )

            self.assertEqual(result["assets_ready"], 1)
            self.assertEqual(result["paths_unlinked"], 0)
            self.assertTrue(trash_path.is_file())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "removed")

    def test_purge_blocks_changed_trash_content(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, _target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            trash_path.write_bytes(b"changed")

            result = lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=True,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )

            self.assertEqual(result["assets_ready"], 0)
            self.assertEqual(len(result["blocked"]), 1)
            self.assertTrue(trash_path.is_file())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "removed")

    def test_hardlinked_trash_reports_no_reclaimable_bytes_when_an_extra_link_remains(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, _source, asset_id, _target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            expected_content = trash_path.read_bytes()
            extra_link = root / "still-referenced.png"
            os.link(trash_path, extra_link)

            result = lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=True,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )

            self.assertGreater(result["logical_bytes"], 0)
            self.assertEqual(result["bytes_expected_reclaimed"], 0)
            self.assertTrue(extra_link.is_file())

            applied = lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=2),
            )

            self.assertEqual(applied["paths_unlinked"], 1)
            self.assertEqual(applied["assets_purged"], 1)
            self.assertTrue(extra_link.is_file())
            self.assertEqual(extra_link.read_bytes(), expected_content)

    def test_purge_rejects_symlinked_managed_namespace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, _source, asset_id, _target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            namespace = library / ".trash" / "mediagent"
            saved_namespace = root / "saved-trash"
            outside = root / "outside-trash"
            namespace.rename(saved_namespace)
            relative = trash_path.relative_to(namespace)
            outside_path = outside / relative
            outside_path.parent.mkdir(parents=True)
            outside_path.write_bytes((saved_namespace / relative).read_bytes())
            namespace.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                lifecycle.purge_trash(
                    db_path,
                    library_root=library,
                    retention_days=0,
                    dry_run=False,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                )

            self.assertTrue(outside_path.is_file())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "removed")

    def test_remove_rolls_files_back_when_database_update_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, target = self._imported(temp_dir)
            original_insert = lifecycle._insert_asset_operation
            original_connect = lifecycle.db.connect
            armed = False
            failed = False

            def insert_and_arm(*args, **kwargs):
                nonlocal armed
                result = original_insert(*args, **kwargs)
                armed = True
                return result

            def fail_next_connect(path):
                nonlocal failed
                if armed and not failed:
                    failed = True
                    raise RuntimeError("simulated database failure")
                return original_connect(path)

            with (
                patch.object(lifecycle, "_insert_asset_operation", side_effect=insert_and_arm),
                patch.object(lifecycle.db, "connect", side_effect=fail_next_connect),
                self.assertRaisesRegex(RuntimeError, "simulated database failure"),
            ):
                lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertTrue(target.is_file())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")

    def test_restore_rolls_files_back_when_database_update_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            original_insert = lifecycle._insert_asset_operation
            original_connect = lifecycle.db.connect
            armed = False
            failed = False

            def insert_and_arm(*args, **kwargs):
                nonlocal armed
                result = original_insert(*args, **kwargs)
                armed = True
                return result

            def fail_next_connect(path):
                nonlocal failed
                if armed and not failed:
                    failed = True
                    raise RuntimeError("simulated database failure")
                return original_connect(path)

            with (
                patch.object(lifecycle, "_insert_asset_operation", side_effect=insert_and_arm),
                patch.object(lifecycle.db, "connect", side_effect=fail_next_connect),
                self.assertRaisesRegex(RuntimeError, "simulated database failure"),
            ):
                lifecycle.restore_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertFalse(target.exists())
            self.assertTrue(trash_path.is_file())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "removed")

    def test_remove_keeps_journal_recoverable_when_filesystem_rollback_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, target = self._imported(temp_dir)
            original_insert = lifecycle._insert_asset_operation
            original_connect = lifecycle.db.connect
            armed = False
            failed = False

            def insert_and_arm(*args, **kwargs):
                nonlocal armed
                result = original_insert(*args, **kwargs)
                armed = True
                return result

            def fail_next_connect(path):
                nonlocal failed
                if armed and not failed:
                    failed = True
                    raise RuntimeError("simulated database failure")
                return original_connect(path)

            with (
                patch.object(lifecycle, "_insert_asset_operation", side_effect=insert_and_arm),
                patch.object(lifecycle.db, "connect", side_effect=fail_next_connect),
                patch.object(lifecycle, "_rollback_moves", return_value=[str(target)]),
                self.assertRaisesRegex(RuntimeError, "rollback was incomplete"),
            ):
                lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)

            with db.connect(db_path) as connection:
                operation = connection.execute(
                    "SELECT state FROM asset_operations ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(operation["state"], "planned")
            self.assertFalse(target.exists())

            retried = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertEqual(retried["result"], "removed")
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "removed")

    def test_restore_keeps_journal_recoverable_when_filesystem_rollback_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            original_insert = lifecycle._insert_asset_operation
            original_connect = lifecycle.db.connect
            armed = False
            failed = False

            def insert_and_arm(*args, **kwargs):
                nonlocal armed
                result = original_insert(*args, **kwargs)
                armed = True
                return result

            def fail_next_connect(path):
                nonlocal failed
                if armed and not failed:
                    failed = True
                    raise RuntimeError("simulated database failure")
                return original_connect(path)

            with (
                patch.object(lifecycle, "_insert_asset_operation", side_effect=insert_and_arm),
                patch.object(lifecycle.db, "connect", side_effect=fail_next_connect),
                patch.object(lifecycle, "_rollback_moves", return_value=[str(trash_path)]),
                self.assertRaisesRegex(RuntimeError, "rollback was incomplete"),
            ):
                lifecycle.restore_asset(db_path, asset_id=asset_id, library_root=library)

            with db.connect(db_path) as connection:
                operation = connection.execute(
                    "SELECT state FROM asset_operations WHERE operation_type = 'restore'"
                ).fetchone()
            self.assertEqual(operation["state"], "planned")
            self.assertTrue(target.is_file())
            self.assertFalse(trash_path.exists())

            retried = lifecycle.restore_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertEqual(retried["result"], "restored")
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")

    def test_asset_merge_preserves_operation_recovery_lineage(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, db_path, context, _source, old_asset_id, _target = self._imported(temp_dir)
            second = root / "incoming" / "second.png"
            Image.new("RGB", (4, 4), color="red").save(second)
            imported = self._run_import(context, second)
            canonical_id = str(imported.data["results"][0]["asset_id"])
            lifecycle._insert_asset_operation(
                db_path,
                operation_id="planned-before-merge",
                asset_id=old_asset_id,
                operation_type="remove",
                state="planned",
                reason=None,
                metadata={"plans": []},
                created_at=datetime.now(UTC).isoformat(),
            )

            with db.connect(db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                assets._merge_assets(
                    connection,
                    canonical_id=canonical_id,
                    merged_id=old_asset_id,
                    now=datetime.now(UTC).isoformat(),
                )
                operation_asset = connection.execute(
                    "SELECT asset_id FROM asset_operations WHERE id = 'planned-before-merge'"
                ).fetchone()[0]

            self.assertEqual(str(operation_asset), canonical_id)
            self.assertIn(old_asset_id, lifecycle._asset_lineage_ids(db_path, canonical_id))

    def test_incomplete_operation_rejects_an_unrelated_journal_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, source, asset_id, target = self._imported(temp_dir)
            decoy = library / "decoy.png"
            decoy.write_bytes(source.read_bytes())
            operation_id = "arm_untrusted"
            fake_target = library / ".trash" / "mediagent" / operation_id / "fake" / decoy.name
            lifecycle._insert_asset_operation(
                db_path,
                operation_id=operation_id,
                asset_id=asset_id,
                operation_type="remove",
                state="planned",
                reason=None,
                metadata={
                    "plans": [
                        {
                            "entry_id": "fake",
                            "source": str(decoy),
                            "target": str(fake_target),
                            "checksum": library_content.sha256_checksum(decoy)[0],
                        }
                    ]
                },
                created_at=datetime.now(UTC).isoformat(),
            )

            with self.assertRaisesRegex(RuntimeError, "unrelated entry"):
                lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertTrue(target.is_file())
            self.assertTrue(decoy.is_file())
            self.assertFalse(fake_target.exists())

    def test_incomplete_purge_journal_recovers_missing_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, _target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            original_mark = lifecycle._mark_entry_purged
            failed = False

            def fail_once(*args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise RuntimeError("simulated database failure")
                return original_mark(*args, **kwargs)

            with (
                patch.object(lifecycle, "_mark_entry_purged", side_effect=fail_once),
                self.assertRaisesRegex(RuntimeError, "simulated database failure"),
            ):
                lifecycle.purge_trash(
                    db_path,
                    library_root=library,
                    retention_days=0,
                    dry_run=False,
                    now=datetime.now(UTC) + timedelta(seconds=1),
                )

            self.assertFalse(trash_path.exists())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "removed")

            lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=2),
            )

            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "purged")

    def test_incomplete_purge_rejects_an_unrelated_asset_entry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, context, _source, first_asset_id, _target = self._imported(temp_dir)
            second_source = root / "incoming" / "second.png"
            Image.new("RGB", (4, 4), color="red").save(second_source)
            second_import = self._run_import(context, second_source)
            second_asset_id = str(second_import.data["results"][0]["asset_id"])
            lifecycle.remove_asset(db_path, asset_id=first_asset_id, library_root=library)
            second_removed = lifecycle.remove_asset(
                db_path,
                asset_id=second_asset_id,
                library_root=library,
            )
            second_trash = Path(second_removed["trash_paths"][0])
            with db.connect(db_path) as connection:
                second_entry = connection.execute(
                    """
                    SELECT le.id, cb.checksum
                    FROM asset_representations ar
                    JOIN library_entries le ON le.id = ar.library_entry_id
                    JOIN content_blobs cb ON cb.id = le.content_blob_id
                    WHERE ar.asset_id = ?
                    """,
                    (second_asset_id,),
                ).fetchone()
            lifecycle._insert_asset_operation(
                db_path,
                operation_id="prg_unrelated",
                asset_id=first_asset_id,
                operation_type="purge",
                state="planned",
                reason="test",
                metadata={
                    "plans": [
                        {
                            "entry_id": str(second_entry["id"]),
                            "path": str(second_trash),
                            "checksum": str(second_entry["checksum"]),
                        }
                    ]
                },
                created_at=datetime.now(UTC).isoformat(),
            )

            lifecycle._recover_incomplete_purges(
                db_path,
                root=library,
                namespace=library / ".trash" / "mediagent",
                completed_at=datetime.now(UTC).isoformat(),
            )

            self.assertTrue(second_trash.is_file())
            with db.connect(db_path) as connection:
                operation_state = connection.execute(
                    "SELECT state FROM asset_operations WHERE id = 'prg_unrelated'"
                ).fetchone()[0]
            self.assertEqual(str(operation_state), "planned")

    def test_incomplete_remove_journal_is_recovered_before_retry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, target = self._imported(temp_dir)
            original_insert = lifecycle._insert_asset_operation
            original_connect = lifecycle.db.connect
            armed = False
            interrupted = False

            def insert_and_arm(*args, **kwargs):
                nonlocal armed
                result = original_insert(*args, **kwargs)
                armed = True
                return result

            def interrupt_next_connect(path):
                nonlocal interrupted
                if armed and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt
                return original_connect(path)

            with (
                patch.object(lifecycle, "_insert_asset_operation", side_effect=insert_and_arm),
                patch.object(lifecycle.db, "connect", side_effect=interrupt_next_connect),
                self.assertRaises(KeyboardInterrupt),
            ):
                lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertFalse(target.exists())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")

            retried = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertEqual(retried["result"], "removed")
            self.assertFalse(target.exists())
            self.assertTrue(Path(retried["trash_paths"][0]).is_file())

    def test_restore_duplicate_cleanup_recovers_after_interruption(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, source, asset_id, target = self._imported(temp_dir)
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

            with (
                patch.object(lifecycle, "_finish_restore_duplicate_cleanup", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                lifecycle.restore_asset(db_path, asset_id=asset_id, library_root=library)

            self.assertTrue(target.is_file())
            self.assertTrue(trash_path.is_file())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")
            recovered = lifecycle.restore_asset(
                db_path,
                asset_id=asset_id,
                library_root=library,
            )
            with db.connect(db_path) as connection:
                operation_state = connection.execute(
                    "SELECT state FROM asset_operations WHERE operation_type = 'restore'"
                ).fetchone()[0]

            self.assertEqual(recovered["result"], "already_active")
            self.assertFalse(trash_path.exists())
            self.assertEqual(str(operation_state), "completed")

    def test_new_url_with_purged_bytes_is_discarded_and_linked_to_tombstone(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, source, asset_id, _target = self._imported(temp_dir)
            content = source.read_bytes()
            lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            url = "https://93.184.216.34/new-source.png"
            http = StaticMediaHttpClient(url, content)
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root / "data"),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                },
                http_client=http,
            )

            result = asyncio.run(
                create_default_registry().run(
                    "link.media.sync",
                    {"url": url},
                    context,
                )
            )

            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(result.data["summary"]["skipped"], 1)
            self.assertEqual(result.data["summary"]["files_skipped"], 1)
            self.assertEqual(result.data["asset_ids"], [asset_id])
            self.assertEqual(list(library.rglob("*.png")), [])
            self.assertEqual(assets.load_asset(db_path, asset_id)["source_count"], 2)

    def test_tombstone_suppression_never_unlinks_through_a_symlink(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, source, asset_id, _target = self._imported(temp_dir)
            lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            outside = root / "outside.png"
            outside.write_bytes(source.read_bytes())
            linked = library / "example" / "linked.png"
            linked.parent.mkdir(parents=True)
            linked.symlink_to(outside)
            checksum, size = library_content.sha256_checksum(outside)
            db.upsert_media_item(
                db_path,
                {
                    "platform": "example",
                    "remote_id": "symlink-copy",
                    "media_type": "photo",
                    "status": "downloading",
                    "metadata": {},
                },
            )
            record = db.upsert_media_file(
                db_path,
                platform="example",
                remote_id="symlink-copy",
                file_key="source:0",
                remote_url="https://example.test/linked.png",
                local_path=str(linked),
                mime_type="image/png",
                size_bytes=size,
                checksum=checksum,
                status="downloaded",
                library_relative_path="example/linked.png",
                storage_layout="test",
                file_health="valid",
            )

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                library_content.adopt_media_file(db_path, file_id=int(record["id"]))

            self.assertTrue(linked.is_symlink())
            self.assertTrue(outside.is_file())

    def test_new_url_with_removed_bytes_does_not_implicitly_restore(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, source, asset_id, target = self._imported(temp_dir)
            content = source.read_bytes()
            removed = lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            trash_path = Path(removed["trash_paths"][0])
            url = "https://93.184.216.34/another-source.png"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root / "data"),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                },
                http_client=StaticMediaHttpClient(url, content),
            )

            result = asyncio.run(
                create_default_registry().run("link.media.sync", {"url": url}, context)
            )

            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(result.data["summary"]["skipped"], 1)
            self.assertFalse(target.exists())
            self.assertTrue(trash_path.is_file())
            removed_asset = assets.load_asset(db_path, asset_id)
            self.assertEqual(removed_asset["state"], "removed")
            self.assertEqual(removed_asset["source_count"], 2)

    def test_tombstoned_representation_in_active_asset_still_blocks_readd(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, source, asset_id, target = self._imported(temp_dir)
            with db.connect(db_path) as connection:
                source_row = connection.execute(
                    "SELECT media_item_id FROM asset_sources WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
                original = connection.execute(
                    """
                    SELECT le.id AS entry_id, mf.id AS file_id
                    FROM asset_representations ar
                    JOIN library_entries le ON le.id = ar.library_entry_id
                    JOIN media_files mf ON mf.library_entry_id = le.id
                    WHERE ar.asset_id = ?
                    """,
                    (asset_id,),
                ).fetchone()
            unique_path = library / "local" / "photo" / "unique.png"
            unique_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (4, 4), color="green").save(unique_path)
            unique_checksum, unique_size = library_content.sha256_checksum(unique_path)
            with db.connect(db_path) as connection:
                remote_id = connection.execute(
                    "SELECT remote_id FROM media_items WHERE id = ?",
                    (int(source_row["media_item_id"]),),
                ).fetchone()[0]
            unique_file = db.upsert_media_file(
                db_path,
                platform="local",
                remote_id=str(remote_id),
                file_key="content:unique-active-representation",
                remote_url=None,
                local_path=str(unique_path),
                mime_type="image/png",
                size_bytes=unique_size,
                checksum=unique_checksum,
                status="downloaded",
                library_relative_path="local/photo/unique.png",
                storage_layout="test",
                file_health="valid",
            )
            adopted = library_content.adopt_media_file(db_path, file_id=int(unique_file["id"]))
            self.assertEqual(str(adopted["asset_id"]), asset_id)
            target.unlink()
            now = datetime.now(UTC).isoformat()
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    UPDATE library_entries
                    SET state = 'purged', trash_path = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, str(original["entry_id"])),
                )
                connection.execute(
                    "UPDATE asset_representations SET active = 0 WHERE library_entry_id = ?",
                    (str(original["entry_id"]),),
                )
                connection.execute(
                    """
                    UPDATE media_files
                    SET status = 'skipped', file_health = 'purged', local_path = NULL
                    WHERE id = ?
                    """,
                    (int(original["file_id"]),),
                )

            url = "https://93.184.216.34/mixed-asset-copy.png"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root / "data"),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                },
                http_client=StaticMediaHttpClient(url, source.read_bytes()),
            )
            result = asyncio.run(
                create_default_registry().run("link.media.sync", {"url": url}, context)
            )

            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(result.data["summary"]["skipped"], 1)
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")
            self.assertEqual(list(library.rglob("mixed-asset-copy.png")), [])
            with db.connect(db_path) as connection:
                new_item = connection.execute(
                    """
                    SELECT mi.id
                    FROM media_items mi
                    JOIN media_files mf ON mf.media_item_id = mi.id
                    WHERE mf.remote_url = ?
                    """,
                    (url,),
                ).fetchone()
                source_link = connection.execute(
                    "SELECT asset_id FROM asset_sources WHERE media_item_id = ?",
                    (int(new_item["id"]),),
                ).fetchone()
            self.assertIsNone(source_link)

            local_copy = root / "incoming" / "mixed-asset-copy.png"
            local_copy.write_bytes(source.read_bytes())
            blocked = self._run_import(context, local_copy)

            self.assertFalse(blocked.is_success)
            self.assertEqual(blocked.error.code, "local_content_removed")
            self.assertEqual(blocked.data["summary"]["blocked"], 1)
            self.assertEqual(assets.load_asset(db_path, asset_id)["source_count"], 1)

    def test_active_asset_entry_can_be_purged_and_remaining_content_restored(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, _context, _source, asset_id, _target = self._imported(temp_dir)
            with db.connect(db_path) as connection:
                original = connection.execute(
                    """
                    SELECT ar.library_entry_id, source.media_item_id, mi.remote_id
                    FROM asset_representations ar
                    JOIN asset_sources source ON source.asset_id = ar.asset_id
                    JOIN media_items mi ON mi.id = source.media_item_id
                    WHERE ar.asset_id = ?
                    """,
                    (asset_id,),
                ).fetchone()
            remaining = library / "local" / "photo" / "remaining.png"
            remaining.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (4, 4), color="green").save(remaining)
            checksum, size = library_content.sha256_checksum(remaining)
            remaining_file = db.upsert_media_file(
                db_path,
                platform="local",
                remote_id=str(original["remote_id"]),
                file_key="content:remaining",
                remote_url=None,
                local_path=str(remaining),
                mime_type="image/png",
                size_bytes=size,
                checksum=checksum,
                status="downloaded",
                library_relative_path="local/photo/remaining.png",
                storage_layout="test",
                file_health="valid",
            )
            adopted = library_content.adopt_media_file(
                db_path,
                file_id=int(remaining_file["id"]),
            )
            self.assertEqual(str(adopted["asset_id"]), asset_id)
            removed_entry = library_content.remove_entry(
                db_path,
                entry_id=str(original["library_entry_id"]),
                library_root=library,
            )
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")
            self.assertIsNone(assets.load_asset(db_path, asset_id)["removed_at"])

            purged = lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )

            self.assertEqual(purged["paths_unlinked"], 1)
            self.assertEqual(purged["assets_purged"], 0)
            self.assertFalse(Path(removed_entry["trash_path"]).exists())
            self.assertEqual(assets.load_asset(db_path, asset_id)["state"], "active")

            lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            restored = lifecycle.restore_asset(
                db_path,
                asset_id=asset_id,
                library_root=library,
            )

            self.assertEqual(restored["purged_representations"], 1)
            self.assertTrue(remaining.is_file())
            refreshed = assets.load_asset(db_path, asset_id)
            self.assertEqual(refreshed["state"], "active")
            self.assertIsNone(refreshed["removed_at"])
            self.assertIsNone(refreshed["purged_at"])

    def test_multifile_source_cannot_revive_a_purged_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, source, purged_asset_id, _target = self._imported(temp_dir)
            lifecycle.remove_asset(db_path, asset_id=purged_asset_id, library_root=library)
            lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            item = db.upsert_media_item(
                db_path,
                {
                    "platform": "example",
                    "remote_id": "multi-file",
                    "media_type": "photo",
                    "status": "downloading",
                    "metadata": {},
                },
            )
            unique_path = library / "example" / "unique.png"
            unique_path.parent.mkdir(parents=True)
            Image.new("RGB", (4, 4), color="green").save(unique_path)
            unique_checksum, unique_size = library_content.sha256_checksum(unique_path)
            unique_file = db.upsert_media_file(
                db_path,
                platform="example",
                remote_id="multi-file",
                file_key="page:1",
                remote_url="https://example.test/1.png",
                local_path=str(unique_path),
                mime_type="image/png",
                size_bytes=unique_size,
                checksum=unique_checksum,
                status="downloaded",
                library_relative_path="example/unique.png",
                storage_layout="test",
                file_health="valid",
            )
            active = library_content.adopt_media_file(db_path, file_id=int(unique_file["id"]))
            active_asset_id = str(active["asset_id"])
            duplicate_path = library / "example" / "purged-copy.png"
            duplicate_path.write_bytes(source.read_bytes())
            duplicate_checksum, duplicate_size = library_content.sha256_checksum(duplicate_path)
            duplicate_file = db.upsert_media_file(
                db_path,
                platform="example",
                remote_id="multi-file",
                file_key="page:2",
                remote_url="https://example.test/2.png",
                local_path=str(duplicate_path),
                mime_type="image/png",
                size_bytes=duplicate_size,
                checksum=duplicate_checksum,
                status="downloaded",
                library_relative_path="example/purged-copy.png",
                storage_layout="test",
                file_health="valid",
            )

            suppressed = library_content.adopt_media_file(db_path, file_id=int(duplicate_file["id"]))

            self.assertTrue(suppressed["suppressed"])
            self.assertFalse(duplicate_path.exists())
            self.assertEqual(assets.load_asset(db_path, purged_asset_id)["state"], "purged")
            self.assertEqual(assets.load_asset(db_path, active_asset_id)["state"], "active")
            with db.connect(db_path) as connection:
                source_asset_id = connection.execute(
                    "SELECT asset_id FROM asset_sources WHERE media_item_id = ?",
                    (int(item["id"]),),
                ).fetchone()[0]
            self.assertEqual(str(source_asset_id), active_asset_id)

            db.update_media_item_status(
                db_path,
                platform="example",
                remote_id="multi-file",
                status="downloaded",
            )
            resolved_item = {
                "platform": "example",
                "remote_id": "multi-file",
                "media_type": "photo",
                "metadata": {
                    "files": [
                        {"url": "https://example.test/1.png", "page": 1, "part": 1},
                        {"url": "https://example.test/2.png", "page": 2, "part": 2},
                    ]
                },
            }
            statuses = {("example", "multi-file"): "downloaded"}

            link_candidates, link_summary = link_tools._sync_candidates(
                [resolved_item],
                statuses,
                db_path=db_path,
                retry_failed=True,
                repair_missing_files=True,
            )
            pixiv_candidates, pixiv_skipped, _unavailable, pixiv_summary = (
                pixiv_tools._sync_candidates(
                    [resolved_item],
                    statuses,
                    db_path=db_path,
                    retry_failed=True,
                    repair_missing_files=True,
                )
            )
            telegram_candidates, telegram_summary = telegram_tools._sync_candidates(
                [resolved_item],
                statuses,
                db_path=db_path,
                retry_failed=True,
                repair_missing_files=True,
                link_items=True,
            )

            self.assertEqual(link_candidates, [])
            self.assertEqual(link_summary["repair_items"], 0)
            self.assertEqual(pixiv_candidates, [])
            self.assertEqual(pixiv_skipped, 1)
            self.assertEqual(pixiv_summary["repair_items"], 0)
            self.assertEqual(telegram_candidates, [])
            self.assertEqual(telegram_summary["repair_items"], 0)

    def test_multifile_source_duplicate_first_cannot_revive_purged_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, _context, source, purged_asset_id, _target = self._imported(temp_dir)
            lifecycle.remove_asset(db_path, asset_id=purged_asset_id, library_root=library)
            lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            item = db.upsert_media_item(
                db_path,
                {
                    "platform": "example",
                    "remote_id": "duplicate-first",
                    "media_type": "photo",
                    "status": "downloading",
                    "metadata": {},
                },
            )
            duplicate_path = library / "example" / "duplicate-first.png"
            duplicate_path.parent.mkdir(parents=True)
            duplicate_path.write_bytes(source.read_bytes())
            checksum, size = library_content.sha256_checksum(duplicate_path)
            duplicate_file = db.upsert_media_file(
                db_path,
                platform="example",
                remote_id="duplicate-first",
                file_key="page:1",
                remote_url="https://example.test/duplicate.png",
                local_path=str(duplicate_path),
                mime_type="image/png",
                size_bytes=size,
                checksum=checksum,
                status="downloaded",
                library_relative_path="example/duplicate-first.png",
                storage_layout="test",
                file_health="valid",
            )
            suppressed = library_content.adopt_media_file(
                db_path,
                file_id=int(duplicate_file["id"]),
            )
            self.assertTrue(suppressed["suppressed"])

            unique_path = library / "example" / "unique-second.png"
            Image.new("RGB", (4, 4), color="orange").save(unique_path)
            unique_checksum, unique_size = library_content.sha256_checksum(unique_path)
            unique_file = db.upsert_media_file(
                db_path,
                platform="example",
                remote_id="duplicate-first",
                file_key="page:2",
                remote_url="https://example.test/unique.png",
                local_path=str(unique_path),
                mime_type="image/png",
                size_bytes=unique_size,
                checksum=unique_checksum,
                status="downloaded",
                library_relative_path="example/unique-second.png",
                storage_layout="test",
                file_health="valid",
            )
            active = library_content.adopt_media_file(db_path, file_id=int(unique_file["id"]))
            db.initialize_database(db_path)

            self.assertEqual(assets.load_asset(db_path, purged_asset_id)["state"], "purged")
            self.assertEqual(assets.load_asset(db_path, str(active["asset_id"]))["state"], "active")
            with db.connect(db_path) as connection:
                source_asset_id = connection.execute(
                    "SELECT asset_id FROM asset_sources WHERE media_item_id = ?",
                    (int(item["id"]),),
                ).fetchone()[0]
            self.assertEqual(str(source_asset_id), str(active["asset_id"]))

    def test_local_import_blocks_cross_role_purged_checksum(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, db_path, context, source, asset_id, _target = self._imported(temp_dir)
            lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE asset_representations SET representation_role = 'source_page' WHERE asset_id = ?",
                    (asset_id,),
                )

            blocked = self._run_import(context, source)

            self.assertFalse(blocked.is_success)
            self.assertEqual(blocked.error.code, "local_content_removed")
            self.assertEqual(blocked.data["summary"]["blocked"], 1)
            self.assertEqual(blocked.artifacts, [])

    def test_comic_packaging_skips_fully_purged_content(self) -> None:
        with TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "library"
            library.mkdir()
            plan = _comic_package_plan(
                item={
                    "platform": "pixiv",
                    "remote_id": "purged-comic",
                    "status": "downloaded",
                    "metadata": {"work_type": "comic"},
                    "files": [
                        {
                            "id": 1,
                            "library_entry_id": "entry-1",
                            "library_state": "purged",
                            "file_health": "purged",
                        }
                    ],
                },
                library_root=library,
                include_platform_layer=True,
                overwrite=False,
                migrate_legacy=False,
            )

            self.assertEqual(plan["status"], "skipped")
            self.assertEqual(plan["reason"], "comic content was permanently purged")

    def test_comic_packaging_skips_suppressed_archive_and_partial_source(self) -> None:
        with TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "library"
            library.mkdir()
            base_item = {
                "platform": "pixiv",
                "remote_id": "suppressed-comic",
                "status": "downloaded",
                "metadata": {"work_type": "comic"},
            }
            suppressed_archive = _comic_package_plan(
                item={
                    **base_item,
                    "files": [
                        {
                            "id": 1,
                            "mime_type": "application/vnd.comicbook+zip",
                            "status": "skipped",
                            "file_health": "purged",
                            "library_entry_id": None,
                        }
                    ],
                },
                library_root=library,
                include_platform_layer=True,
                overwrite=False,
                migrate_legacy=False,
            )
            partial_source = _comic_package_plan(
                item={
                    **base_item,
                    "files": [
                        {
                            "id": 2,
                            "mime_type": "image/png",
                            "status": "downloaded",
                            "file_health": "valid",
                            "local_path": str(library / "healthy.png"),
                        },
                        {
                            "id": 3,
                            "mime_type": "image/png",
                            "status": "skipped",
                            "file_health": "removed",
                            "library_entry_id": None,
                        },
                    ],
                },
                library_root=library,
                include_platform_layer=True,
                overwrite=False,
                migrate_legacy=False,
            )

            self.assertEqual(suppressed_archive["status"], "skipped")
            self.assertEqual(suppressed_archive["reason"], "comic archive was permanently purged")
            self.assertEqual(partial_source["status"], "skipped")
            self.assertEqual(partial_source["reason"], "comic source content was explicitly removed")

    def test_known_purged_source_is_skipped_before_download(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            data = root / "data"
            db_path = data / "mediagent.sqlite3"
            url = "https://93.184.216.34/known-source.jpg"
            http = StaticMediaHttpClient(url, b"same-content", mime_type="image/jpeg")
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(data),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                },
                http_client=http,
            )
            registry = create_default_registry()
            first = asyncio.run(registry.run("link.media.sync", {"url": url}, context))
            self.assertTrue(first.is_success, first.to_dict())
            asset_id = first.data["asset_ids"][0]
            lifecycle.remove_asset(db_path, asset_id=asset_id, library_root=library)
            lifecycle.purge_trash(
                db_path,
                library_root=library,
                retention_days=0,
                dry_run=False,
                now=datetime.now(UTC) + timedelta(seconds=1),
            )
            http.get_calls = 0

            second = asyncio.run(registry.run("link.media.sync", {"url": url}, context))

            self.assertTrue(second.is_success, second.to_dict())
            self.assertEqual(second.data["summary"]["blocked_purged"], 1)
            self.assertEqual(second.data["summary"]["queued"], 0)
            self.assertEqual(http.get_calls, 0)

    def _imported(
        self,
        temp_dir: str,
    ) -> tuple[Path, Path, Path, ToolContext, Path, str, Path]:
        root = Path(temp_dir)
        library = root / "library"
        data = root / "data"
        db_path = data / "mediagent.sqlite3"
        source = root / "incoming" / "sample.png"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (4, 4), color="blue").save(source)
        context = ToolContext.from_env(
            cwd=root,
            env={
                "MEDIAGENT_DATA_DIR": str(data),
                "MEDIAGENT_DB_PATH": str(db_path),
                "MEDIAGENT_LIBRARY_DIR": str(library),
            },
        )
        imported = self._run_import(context, source)
        self.assertTrue(imported.is_success, imported.to_dict())
        result = imported.data["results"][0]
        return root, library, db_path, context, source, result["asset_id"], Path(result["target_path"])

    def _run_import(self, context: ToolContext, source: Path):
        return asyncio.run(
            create_default_registry().run(
                "media.local.import",
                {"path": str(source)},
                context,
            )
        )


class StaticMediaHttpClient:
    def __init__(self, url: str, content: bytes, *, mime_type: str = "image/png") -> None:
        self.url = url
        self.content = content
        self.mime_type = mime_type
        self.get_calls = 0

    def head(self, url: str, *, headers=None, timeout=30.0):
        return HttpResponse(
            200,
            {"Content-Type": self.mime_type, "Content-Length": str(len(self.content))},
            b"",
            url,
        )

    def get_limited(self, url: str, *, headers=None, timeout=30.0, max_bytes=1024 * 1024):
        self.get_calls += 1
        return HttpResponse(
            200,
            {"Content-Type": self.mime_type, "Content-Length": str(len(self.content))},
            self.content[:max_bytes],
            url,
        )


if __name__ == "__main__":
    unittest.main()
