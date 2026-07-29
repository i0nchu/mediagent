import asyncio
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core import db
from mediagent.core.tooling import ToolContext
from mediagent.tools.defaults import create_default_registry


class CleanupToolTests(unittest.TestCase):
    def test_cleanup_plan_does_not_mutate_files_or_db(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            media_path = data_dir / "library" / "pixiv" / "photo" / "2026" / "01" / "sample.jpg"
            _seed_media_record(db_path=db_path, media_path=media_path)

            result = asyncio.run(
                registry.run(
                    "core.cleanup.media_state",
                    {"mode": "plan", "platform": "pixiv", "remote_id": "item-1"},
                    context,
                )
            )

            self.assertTrue(result.is_success)
            self.assertTrue(media_path.exists())
            with sqlite3.connect(db_path) as connection:
                item_row = connection.execute(
                    "SELECT status FROM media_items WHERE platform = 'pixiv' AND remote_id = 'item-1'"
                ).fetchone()
                file_count = connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
            self.assertEqual(item_row[0], "downloaded")
            self.assertEqual(file_count, 1)
            self.assertEqual(result.data["manifest"]["summary"]["selected_items"], 1)
            self.assertEqual(result.data["manifest"]["summary"]["existing_files"], 1)

    def test_cleanup_requires_platform_selector(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            _, _, context = _context(temp_dir)

            result = asyncio.run(
                registry.run(
                    "core.cleanup.media_state",
                    {"mode": "plan", "platform": ""},
                    context,
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "missing_selector")

    def test_cleanup_never_includes_credential_paths(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            credential_path = data_dir / "credentials" / "pixiv-oauth.json"
            credential_path.parent.mkdir(parents=True, exist_ok=True)
            credential_path.write_text('{"refresh_token":"secret"}\n', encoding="utf-8")
            _seed_media_record(
                db_path=db_path,
                media_path=credential_path,
                local_path=str(credential_path),
                remote_id="cred-1",
            )

            result = asyncio.run(
                registry.run(
                    "core.cleanup.media_state",
                    {"mode": "plan", "platform": "pixiv", "remote_id": "cred-1"},
                    context,
                )
            )

            self.assertTrue(result.is_success)
            file_entry = result.data["manifest"]["items"][0]["media_files"][0]
            self.assertIsNone(file_entry["source_path"])
            self.assertEqual(file_entry["skip_reason"], "protected credential path")
            self.assertNotIn(str(credential_path), str(result.to_dict()))

    def test_cleanup_apply_quarantines_before_db_reset(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            media_path = data_dir / "library" / "pixiv" / "photo" / "2026" / "01" / "apply.jpg"
            _seed_media_record(db_path=db_path, media_path=media_path)

            result = asyncio.run(
                registry.run(
                    "core.cleanup.media_state",
                    {"mode": "apply", "platform": "pixiv", "remote_id": "item-1", "confirm": True},
                    context,
                )
            )

            self.assertTrue(result.is_success)
            quarantine_path = Path(
                result.data["manifest"]["items"][0]["media_files"][0]["quarantine_path"]
            )
            self.assertFalse(media_path.exists())
            self.assertTrue(quarantine_path.exists())
            with sqlite3.connect(db_path) as connection:
                item_row = connection.execute(
                    "SELECT status, downloaded_at FROM media_items WHERE platform = 'pixiv' AND remote_id = 'item-1'"
                ).fetchone()
                file_count = connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
            self.assertEqual(item_row[0], "discovered")
            self.assertIsNone(item_row[1])
            self.assertEqual(file_count, 0)
            self.assertEqual(result.data["manifest"]["summary"]["moved_files"], 1)
            self.assertEqual(result.data["manifest"]["summary"]["reset_items"], 1)

    def test_cleanup_apply_requires_explicit_confirmation(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir, db_path, context = _context(temp_dir)
            media_path = data_dir / "library" / "pixiv" / "photo" / "2026" / "01" / "apply.jpg"
            _seed_media_record(db_path=db_path, media_path=media_path)

            result = asyncio.run(
                registry.run(
                    "core.cleanup.media_state",
                    {"mode": "apply", "platform": "pixiv", "remote_id": "item-1"},
                    context,
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "cleanup_not_confirmed")
            self.assertTrue(media_path.exists())
            with sqlite3.connect(db_path) as connection:
                item_row = connection.execute(
                    "SELECT status FROM media_items WHERE platform = 'pixiv' AND remote_id = 'item-1'"
                ).fetchone()
                file_count = connection.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
            self.assertEqual(item_row[0], "downloaded")
            self.assertEqual(file_count, 1)

    def test_cleanup_rejects_unsafe_quarantine_dir(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            _, db_path, context = _context(temp_dir)
            _seed_media_record(
                db_path=db_path,
                media_path=Path(temp_dir) / "data" / "library" / "pixiv" / "photo" / "2026" / "01" / "sample.jpg",
            )

            result = asyncio.run(
                registry.run(
                    "core.cleanup.media_state",
                    {
                        "mode": "plan",
                        "platform": "pixiv",
                        "quarantine_dir": str(Path(temp_dir).parent / "outside-quarantine"),
                    },
                    context,
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "unsafe_path")


def _context(temp_dir: str) -> tuple[Path, Path, ToolContext]:
    data_dir = Path(temp_dir) / "data"
    db_path = data_dir / "mediagent.sqlite3"
    env = {
        "MEDIAGENT_DATA_DIR": str(data_dir),
        "MEDIAGENT_DB_PATH": str(db_path),
        "PIXIV_CREDENTIALS_FILE": str(data_dir / "credentials" / "pixiv-oauth.json"),
    }
    context = ToolContext.from_env(env=env, cwd=Path(temp_dir))
    return data_dir, db_path, context


def _seed_media_record(
    *,
    db_path: Path,
    media_path: Path,
    remote_id: str = "item-1",
    local_path: str | None = None,
) -> None:
    media_path.parent.mkdir(parents=True, exist_ok=True)
    if not media_path.exists():
        media_path.write_bytes(b"test")
    db.initialize_database(db_path)
    db.upsert_media_item(
        db_path,
        {
            "platform": "pixiv",
            "remote_id": remote_id,
            "media_type": "photo",
            "status": "downloaded",
        },
    )
    library_relative_path = None
    if local_path is None:
        library_relative_path = str(media_path.relative_to(db_path.parent / "library")).replace("\\", "/")
        local_path = str(media_path)
    db.upsert_media_file(
        db_path,
        platform="pixiv",
        remote_id=remote_id,
        remote_url=f"https://example.invalid/{remote_id}.jpg",
        local_path=local_path,
        mime_type="image/jpeg",
        size_bytes=media_path.stat().st_size if media_path.exists() else None,
        checksum=None,
        status="downloaded",
        library_relative_path=library_relative_path,
        storage_layout="scanner-friendly-v2" if library_relative_path else None,
        file_health="valid",
    )
