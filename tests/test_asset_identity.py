import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core import assets, db, library_content


class AssetIdentityTests(unittest.TestCase):
    def test_exact_general_content_from_two_sources_is_one_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            first = root / "pixiv" / "same.jpg"
            second = root / "telegram" / "same.jpg"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"same-visible-image")
            second.write_bytes(b"same-visible-image")

            first_result = self._adopt(
                db_path,
                first,
                platform="pixiv",
                remote_id="work-1",
                title="Canonical title",
                author_name="Artist",
            )
            second_result = self._adopt(db_path, second, platform="telegram", remote_id="message-2")

            self.assertEqual(first_result["asset_id"], second_result["asset_id"])
            asset = assets.load_asset(db_path, first_result["asset_id"])
            self.assertEqual(asset["media_type"], "image")
            self.assertEqual(asset["state"], "active")
            self.assertEqual(asset["source_count"], 2)
            self.assertEqual(asset["representation_count"], 1)
            self.assertEqual(asset["metadata"]["title"], "Canonical title")
            self.assertEqual(asset["metadata"]["author_name"], "Artist")
            self.assertEqual(asset["metadata"]["tags"], [])

    def test_comic_pages_and_archive_from_one_source_share_one_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            page = root / "comic-pages" / "jmcomic" / "chapter" / "001.jpg"
            archive = root / "comic" / "jmcomic" / "chapter.cbz"
            page.parent.mkdir(parents=True)
            archive.parent.mkdir(parents=True)
            page.write_bytes(b"comic-page")
            archive.write_bytes(b"comic-archive")

            page_result = self._adopt(
                db_path,
                page,
                platform="jmcomic",
                remote_id="photo:chapter",
                file_key="page:1",
                mime_type="image/jpeg",
                relative_path="comic-pages/jmcomic/chapter/001.jpg",
            )
            archive_result = self._adopt(
                db_path,
                archive,
                platform="jmcomic",
                remote_id="photo:chapter",
                file_key="archive:cbz",
                mime_type="application/vnd.comicbook+zip",
                relative_path="comic/jmcomic/chapter.cbz",
            )

            self.assertEqual(page_result["asset_id"], archive_result["asset_id"])
            asset = assets.load_asset(db_path, page_result["asset_id"])
            self.assertEqual(asset["media_type"], "comic")
            self.assertEqual(asset["source_count"], 1)
            self.assertEqual(asset["representation_count"], 2)
            with db.connect(db_path) as connection:
                roles = {
                    row[0]
                    for row in connection.execute(
                        "SELECT representation_role FROM asset_representations WHERE asset_id = ?",
                        (asset["id"],),
                    )
                }
            self.assertEqual(roles, {"source_page", "comic_archive"})

    def test_equal_comic_page_bytes_do_not_merge_unrelated_chapters(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            first = root / "comic-pages" / "jmcomic" / "a" / "001.jpg"
            second = root / "comic-pages" / "jmcomic" / "b" / "001.jpg"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"shared-page-bytes")
            second.write_bytes(b"shared-page-bytes")

            first_result = self._adopt(
                db_path,
                first,
                platform="jmcomic",
                remote_id="photo:a",
                relative_path="comic-pages/jmcomic/a/001.jpg",
            )
            second_result = self._adopt(
                db_path,
                second,
                platform="jmcomic",
                remote_id="photo:b",
                relative_path="comic-pages/jmcomic/b/001.jpg",
            )

            self.assertNotEqual(first_result["asset_id"], second_result["asset_id"])
            self.assertEqual(assets.load_asset(db_path, first_result["asset_id"])["media_type"], "comic")
            self.assertEqual(assets.load_asset(db_path, second_result["asset_id"])["media_type"], "comic")

    def test_v10_backfill_is_idempotent_and_preserves_removed_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            source = root / "pixiv" / "removed.jpg"
            duplicate = root / "telegram" / "removed-copy.jpg"
            source.parent.mkdir(parents=True)
            duplicate.parent.mkdir(parents=True)
            source.write_bytes(b"removed-content")
            duplicate.write_bytes(b"removed-content")
            adoption = self._adopt(db_path, source, platform="pixiv", remote_id="removed-work")
            duplicate_adoption = self._adopt(
                db_path,
                duplicate,
                platform="telegram",
                remote_id="removed-message",
            )
            self.assertEqual(adoption["entry_id"], duplicate_adoption["entry_id"])
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE library_entries SET state = 'removed' WHERE id = ?",
                    (adoption["entry_id"],),
                )
                connection.execute("DROP TABLE asset_representations")
                connection.execute("DROP TABLE asset_sources")
                connection.execute("DROP TABLE assets")
                connection.execute("UPDATE schema_meta SET value = '10' WHERE key = 'schema_version'")

            migration = db.initialize_database(db_path)
            with db.connect(db_path) as connection:
                first_id = connection.execute("SELECT id FROM assets WHERE state = 'removed'").fetchone()[0]
                first_counts = (
                    connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM asset_representations").fetchone()[0],
                )
            db.initialize_database(db_path)
            with db.connect(db_path) as connection:
                second_id = connection.execute("SELECT id FROM assets WHERE state = 'removed'").fetchone()[0]
                second_counts = (
                    connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM asset_representations").fetchone()[0],
                )

            self.assertEqual(db.get_schema_version(db_path), "11")
            self.assertTrue(migration["migrated"])
            self.assertEqual(migration["previous_schema_version"], "10")
            self.assertEqual(migration["asset_backfill"]["assets_created"], 1)
            self.assertEqual(migration["asset_backfill"]["sources_linked"], 2)
            self.assertEqual(migration["asset_backfill"]["representations_linked"], 1)
            self.assertEqual(first_id, second_id)
            self.assertEqual(first_counts, (2, 1))
            self.assertEqual(second_counts, first_counts)

    def test_new_relationship_merges_assets_and_old_id_resolves(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            first = root / "one" / "first.jpg"
            second = root / "two" / "second.jpg"
            bridge = root / "one" / "bridge.jpg"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"first-content")
            second.write_bytes(b"shared-content")
            bridge.write_bytes(b"shared-content")

            first_result = self._adopt(db_path, first, platform="pixiv", remote_id="multi")
            second_result = self._adopt(db_path, second, platform="telegram", remote_id="single")
            self.assertNotEqual(first_result["asset_id"], second_result["asset_id"])

            bridge_result = self._adopt(
                db_path,
                bridge,
                platform="pixiv",
                remote_id="multi",
                file_key="second-file",
            )

            self.assertEqual(bridge_result["asset_id"], first_result["asset_id"])
            old_alias = assets.load_asset(db_path, second_result["asset_id"])
            self.assertEqual(old_alias["id"], first_result["asset_id"])
            self.assertEqual(old_alias["requested_asset_id"], second_result["asset_id"])
            self.assertEqual(old_alias["source_count"], 2)
            self.assertEqual(old_alias["representation_count"], 2)

    def test_existing_remove_and_restore_keep_asset_state_in_sync(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            source = root / "library" / "wanted.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"managed-lifecycle")
            adoption = self._adopt(db_path, source, platform="pixiv", remote_id="lifecycle")

            removed = library_content.remove_entry(
                db_path,
                entry_id=adoption["entry_id"],
                library_root=root,
            )
            removed_asset = assets.load_asset(db_path, adoption["asset_id"])
            restored = library_content.restore_entry(
                db_path,
                removal_id=removed["removal_id"],
            )
            restored_asset = assets.load_asset(db_path, adoption["asset_id"])

            self.assertEqual(removed_asset["state"], "removed")
            self.assertEqual(removed["entry"]["asset_id"], adoption["asset_id"])
            self.assertEqual(restored_asset["state"], "active")
            self.assertEqual(restored["entry"]["asset_id"], adoption["asset_id"])

    def test_backfill_groups_large_page_sets_without_losing_relationships(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            timestamp = "2026-08-31T00:00:00+00:00"
            with db.connect(db_path) as connection:
                for source_index in range(20):
                    cursor = connection.execute(
                        """
                        INSERT INTO media_items (
                            platform, remote_id, media_type, status,
                            metadata_json, created_at, updated_at
                        ) VALUES ('jmcomic', ?, 'photo', 'downloaded', '{}', ?, ?)
                        """,
                        (f"photo:{source_index}", timestamp, timestamp),
                    )
                    media_item_id = int(cursor.lastrowid)
                    for page_index in range(25):
                        suffix = f"{source_index:02d}{page_index:03d}"
                        blob_id = f"blob_{suffix}"
                        entry_id = f"entry_{suffix}"
                        connection.execute(
                            """
                            INSERT INTO content_blobs (
                                id, checksum, size_bytes, mime_type, created_at, updated_at
                            ) VALUES (?, ?, 1, 'image/jpeg', ?, ?)
                            """,
                            (blob_id, f"sha256:{suffix:0<64}", timestamp, timestamp),
                        )
                        connection.execute(
                            """
                            INSERT INTO library_entries (
                                id, content_blob_id, presentation_key, state,
                                local_path, created_at, updated_at
                            ) VALUES (?, ?, ?, 'active', ?, ?, ?)
                            """,
                            (
                                entry_id,
                                blob_id,
                                f"comic-source:jmcomic:photo:{source_index}:page:{page_index}",
                                str(Path(temp_dir) / f"{suffix}.jpg"),
                                timestamp,
                                timestamp,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO media_files (
                                media_item_id, file_key, local_path, mime_type,
                                size_bytes, checksum, status, library_entry_id
                            ) VALUES (?, ?, ?, 'image/jpeg', 1, ?, 'downloaded', ?)
                            """,
                            (
                                media_item_id,
                                f"page:{page_index}",
                                str(Path(temp_dir) / f"{suffix}.jpg"),
                                f"sha256:{suffix:0<64}",
                                entry_id,
                            ),
                        )
                connection.execute("DROP TABLE asset_representations")
                connection.execute("DROP TABLE asset_sources")
                connection.execute("DROP TABLE assets")
                connection.execute("UPDATE schema_meta SET value = '10' WHERE key = 'schema_version'")

            db.initialize_database(db_path)

            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 20)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0], 20)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM asset_representations").fetchone()[0],
                    500,
                )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_concurrent_attachment_creates_one_asset_identity(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            source = root / "pixiv" / "concurrent.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"concurrent-content")
            adoption = self._adopt(db_path, source, platform="pixiv", remote_id="concurrent")
            with db.connect(db_path) as connection:
                connection.execute("DELETE FROM asset_representations")
                connection.execute("DELETE FROM asset_sources")
                connection.execute("DELETE FROM assets")

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda _index: assets.attach_media_file(
                            db_path,
                            file_id=adoption["file_id"],
                        ),
                        range(2),
                    )
                )

            self.assertEqual(results[0]["id"], results[1]["id"])
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM asset_representations").fetchone()[0],
                    1,
                )

    def _adopt(
        self,
        db_path: Path,
        path: Path,
        *,
        platform: str,
        remote_id: str,
        file_key: str = "main",
        mime_type: str = "image/jpeg",
        relative_path: str | None = None,
        title: str | None = None,
        author_name: str | None = None,
    ) -> dict:
        db.initialize_database(db_path)
        db.upsert_media_item(
            db_path,
            {
                "platform": platform,
                "remote_id": remote_id,
                "source_url": f"https://example.invalid/{platform}/{remote_id}",
                "author_name": author_name,
                "media_type": "photo",
                "status": "downloaded",
                "metadata": {"title": title} if title else {},
            },
        )
        checksum, size_bytes = library_content.sha256_checksum(path)
        record = db.upsert_media_file(
            db_path,
            platform=platform,
            remote_id=remote_id,
            file_key=file_key,
            remote_url=f"https://cdn.example.invalid/{platform}/{remote_id}/{file_key}",
            local_path=str(path),
            mime_type=mime_type,
            size_bytes=size_bytes,
            checksum=checksum,
            status="downloaded",
            library_relative_path=relative_path or str(path.relative_to(path.parents[1])),
            file_health="healthy",
        )
        return library_content.adopt_media_file(db_path, file_id=record["id"])


if __name__ == "__main__":
    unittest.main()
