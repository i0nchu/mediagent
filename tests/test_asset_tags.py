from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core import asset_tags, assets, db, library_content
from mediagent.core.tooling import ToolContext
from mediagent.tools.defaults import create_default_registry


class AssetTagTests(unittest.TestCase):
    def test_tags_are_equal_priority_normalized_and_idempotent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(
                db_path,
                root / "library" / "pixiv" / "work.jpg",
                platform="pixiv",
                remote_id="work-1",
                title="Evening Sky",
                author="Artist",
            )

            first = asset_tags.update_tags(
                db_path,
                asset_id=asset_id,
                add=["Favorite", "favorite", "Cafe\u0301"],
            )
            second = asset_tags.update_tags(
                db_path,
                asset_id=asset_id,
                add=["FAVORITE"],
                remove=["CAFÉ"],
            )

            self.assertEqual(
                first["metadata"]["tags"],
                ["source:pixiv", "type:image", "Favorite", "Café"],
            )
            self.assertEqual(first["tags_added"], ["Favorite", "Café"])
            self.assertEqual(
                second["metadata"]["tags"],
                ["source:pixiv", "type:image", "Favorite"],
            )
            self.assertEqual(second["tags_added"], [])
            self.assertEqual(second["tags_removed"], ["Café"])

    def test_reserved_and_control_tags_are_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(db_path, root / "library" / "file.jpg")

            for tag in ("source:manual", "TYPE:video"):
                with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "maintained automatically"):
                    asset_tags.update_tags(db_path, asset_id=asset_id, add=[tag])
            with self.assertRaisesRegex(ValueError, "control"):
                asset_tags.update_tags(db_path, asset_id=asset_id, add=["bad\ntag"])

    def test_merged_alias_updates_the_canonical_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(db_path, root / "library" / "file.jpg")
            alias_id = "asset_old_alias"
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO assets (
                        id, media_type, state, metadata_json, merged_into_asset_id,
                        created_at, updated_at
                    ) VALUES (?, 'image', 'merged', '{"tags": []}', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (alias_id, asset_id),
                )

            result = asset_tags.update_tags(db_path, asset_id=alias_id, add=["reviewed"])

            self.assertEqual(result["id"], asset_id)
            self.assertEqual(result["metadata"]["tags"][-1], "reviewed")
            self.assertIn("reviewed", assets.load_asset(db_path, alias_id)["metadata"]["tags"])

    def test_asset_merge_unions_tags_without_case_or_unicode_duplicates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            first_id = self._asset(
                db_path,
                root / "library" / "first.jpg",
                remote_id="first",
            )
            second_id = self._asset(
                db_path,
                root / "library" / "second.jpg",
                remote_id="second",
            )
            asset_tags.update_tags(db_path, asset_id=first_id, add=["Favorite", "Café"])
            asset_tags.update_tags(
                db_path,
                asset_id=second_id,
                add=["favorite", "Cafe\u0301", "landscape"],
            )
            with db.connect(db_path) as connection:
                second_media_item_id = int(
                    connection.execute(
                        "SELECT id FROM media_items WHERE platform = 'local' AND remote_id = 'second'"
                    ).fetchone()["id"]
                )

            merged = assets.attach_media_item(
                db_path,
                media_item_id=second_media_item_id,
                asset_id=first_id,
            )

            tags = merged["metadata"]["tags"]
            self.assertEqual(sum(tag.casefold() == "favorite" for tag in tags), 1)
            self.assertEqual(sum(tag.casefold() == "café" for tag in tags), 1)
            self.assertIn("landscape", tags)

    def test_invalid_existing_tags_are_not_silently_rewritten(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(db_path, root / "library" / "file.jpg")
            malformed = '{"tags": ["keep", "bad\\nvalue"]}'
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE assets SET metadata_json = ? WHERE id = ?",
                    (malformed, asset_id),
                )

            with self.assertRaisesRegex(ValueError, "invalid existing tag"):
                asset_tags.update_tags(db_path, asset_id=asset_id, add=["new"])

            with db.connect(db_path) as connection:
                stored = connection.execute(
                    "SELECT metadata_json FROM assets WHERE id = ?", (asset_id,)
                ).fetchone()["metadata_json"]
            self.assertEqual(stored, malformed)

    def test_malformed_asset_metadata_is_not_overwritten_by_tag_mutation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(db_path, root / "library" / "file.jpg")
            malformed = "{broken"
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE assets SET metadata_json = ? WHERE id = ?",
                    (malformed, asset_id),
                )

            with self.assertRaisesRegex(ValueError, "metadata is invalid"):
                asset_tags.update_tags(db_path, asset_id=asset_id, add=["new"])

            with db.connect(db_path) as connection:
                stored = connection.execute(
                    "SELECT metadata_json FROM assets WHERE id = ?", (asset_id,)
                ).fetchone()["metadata_json"]
            self.assertEqual(stored, malformed)

    def test_asset_refresh_rolls_back_when_existing_tags_are_malformed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(db_path, root / "library" / "file.jpg")
            malformed = '{"title": "keep me", "tags": "legacy-tag"}'
            with db.connect(db_path) as connection:
                entry_id = connection.execute(
                    "SELECT primary_library_entry_id FROM assets WHERE id = ?", (asset_id,)
                ).fetchone()["primary_library_entry_id"]
                connection.execute(
                    "UPDATE assets SET metadata_json = ? WHERE id = ?",
                    (malformed, asset_id),
                )

            with self.assertRaisesRegex(ValueError, "list of strings"):
                assets.refresh_for_library_entry(db_path, str(entry_id))

            with db.connect(db_path) as connection:
                stored = connection.execute(
                    "SELECT metadata_json FROM assets WHERE id = ?", (asset_id,)
                ).fetchone()["metadata_json"]
            self.assertEqual(stored, malformed)

    def test_asset_merge_rolls_back_when_member_tags_are_malformed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            first_id = self._asset(
                db_path,
                root / "library" / "first.jpg",
                remote_id="first",
            )
            second_id = self._asset(
                db_path,
                root / "library" / "second.jpg",
                remote_id="second",
            )
            with db.connect(db_path) as connection:
                second_media_item_id = int(
                    connection.execute(
                        "SELECT id FROM media_items WHERE platform = 'local' AND remote_id = 'second'"
                    ).fetchone()["id"]
                )
                original_sources = {
                    int(row["media_item_id"]): str(row["asset_id"])
                    for row in connection.execute(
                        "SELECT media_item_id, asset_id FROM asset_sources"
                    )
                }
                connection.execute(
                    "UPDATE assets SET metadata_json = ? WHERE id = ?",
                    ('{"tags": ["keep", 7]}', second_id),
                )

            with self.assertRaisesRegex(ValueError, "only strings"):
                assets.attach_media_item(
                    db_path,
                    media_item_id=second_media_item_id,
                    asset_id=first_id,
                )

            with db.connect(db_path) as connection:
                states = {
                    str(row["id"]): str(row["state"])
                    for row in connection.execute(
                        "SELECT id, state FROM assets WHERE id IN (?, ?)",
                        (first_id, second_id),
                    )
                }
                sources = {
                    int(row["media_item_id"]): str(row["asset_id"])
                    for row in connection.execute(
                        "SELECT media_item_id, asset_id FROM asset_sources"
                    )
                }
            self.assertEqual(states, {first_id: "active", second_id: "active"})
            self.assertEqual(sources, original_sources)

    def test_search_matches_tags_metadata_sources_and_literal_filename_terms(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            first_id = self._asset(
                db_path,
                root / "library" / "pixiv" / "evening_100%.jpg",
                platform="pixiv",
                remote_id="work-special",
                title="Evening Sky",
                author="Alice",
                description="Blue landscape",
            )
            self._asset(
                db_path,
                root / "library" / "telegram" / "morning.jpg",
                platform="telegram",
                remote_id="message-other",
                title="Morning",
                author="Bob",
            )
            asset_tags.update_tags(db_path, asset_id=first_id, add=["favorite", "landscape", "Café"])

            by_tag_and_author = asset_tags.search(db_path, terms=["favorite", "alice"])
            by_unicode_case = asset_tags.search(db_path, terms=["CAFÉ"])
            by_description = asset_tags.search(db_path, terms=["blue landscape"])
            by_literal_filename = asset_tags.search(db_path, terms=["100%"])
            no_wildcard_expansion = asset_tags.search(db_path, terms=["100_"])
            metadata_key_is_not_content = asset_tags.search(db_path, terms=["description"])

            self.assertEqual([item["asset_id"] for item in by_tag_and_author["assets"]], [first_id])
            self.assertIn("tags", by_tag_and_author["assets"][0]["matched_fields"])
            self.assertIn("author", by_tag_and_author["assets"][0]["matched_fields"])
            self.assertEqual([item["asset_id"] for item in by_unicode_case["assets"]], [first_id])
            self.assertEqual([item["asset_id"] for item in by_description["assets"]], [first_id])
            self.assertIn("source", by_description["assets"][0]["matched_fields"])
            self.assertEqual([item["asset_id"] for item in by_literal_filename["assets"]], [first_id])
            self.assertEqual(no_wildcard_expansion["count"], 0)
            self.assertEqual(metadata_key_is_not_content["count"], 0)

    def test_search_decodes_unicode_and_does_not_match_parent_directories(self) -> None:
        with TemporaryDirectory(prefix="search-parent-marker-") as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(
                db_path,
                root / "library" / "nested" / "work.jpg",
                title="飛機杯女神",
            )
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    UPDATE library_entries
                    SET display_name_override = '閱讀清單.jpg'
                    WHERE id = (
                        SELECT primary_library_entry_id FROM assets WHERE id = ?
                    )
                    """,
                    (asset_id,),
                )

            by_title = asset_tags.search(db_path, terms=["飛機杯女神"])
            by_display_name = asset_tags.search(db_path, terms=["閱讀清單"])
            by_parent = asset_tags.search(db_path, terms=["search-parent-marker"])

            self.assertEqual([item["asset_id"] for item in by_title["assets"]], [asset_id])
            self.assertEqual(
                [item["asset_id"] for item in by_display_name["assets"]], [asset_id]
            )
            self.assertEqual(
                by_display_name["assets"][0]["paths"][0]["display_name"],
                "閱讀清單.jpg",
            )
            self.assertEqual(by_parent["count"], 0)

    def test_search_normalizes_unicode_and_reads_structured_provider_tags(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(
                db_path,
                root / "library" / "work.jpg",
                remote_id="unicode-work",
                title="Cafe\u0301",
            )
            with db.connect(db_path) as connection:
                media_item_id = connection.execute(
                    "SELECT id FROM media_items WHERE remote_id = 'unicode-work'"
                ).fetchone()["id"]
                connection.execute(
                    "UPDATE media_items SET metadata_json = ? WHERE id = ?",
                    ('{"tags": [{"type": "tag", "name": "schoolgirl"}]}', media_item_id),
                )
                connection.execute(
                    """
                    UPDATE library_entries
                    SET display_name_override = ?
                    WHERE id = (
                        SELECT primary_library_entry_id FROM assets WHERE id = ?
                    )
                    """,
                    ("Re\u0301sume\u0301.jpg", asset_id),
                )

            by_title = asset_tags.search(db_path, terms=["Café"])
            by_display_name = asset_tags.search(db_path, terms=["Résumé"])
            by_provider_tag = asset_tags.search(db_path, terms=["schoolgirl"])

            self.assertEqual([item["asset_id"] for item in by_title["assets"]], [asset_id])
            self.assertEqual(
                [item["asset_id"] for item in by_display_name["assets"]], [asset_id]
            )
            self.assertEqual(
                [item["asset_id"] for item in by_provider_tag["assets"]], [asset_id]
            )

    def test_search_defaults_to_active_and_can_include_inactive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(db_path, root / "library" / "hidden.jpg", title="Hidden")
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE assets SET state = 'removed', removed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (asset_id,),
                )

            self.assertEqual(asset_tags.search(db_path, terms=["Hidden"])["count"], 0)
            included = asset_tags.search(db_path, terms=["Hidden"], include_inactive=True)
            self.assertEqual(included["count"], 1)
            self.assertEqual(included["assets"][0]["state"], "removed")

    def test_tools_do_not_create_a_missing_database(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "missing.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(db_path),
                },
            )
            registry = create_default_registry()

            search = asyncio.run(registry.run("library.asset.search", {}, context))
            update = asyncio.run(
                registry.run(
                    "library.asset.tags.update",
                    {"asset_id": "asset_missing", "add": ["tag"]},
                    context,
                )
            )

            self.assertEqual(search.error.code, "missing_db")
            self.assertEqual(update.error.code, "missing_db")
            self.assertFalse(db_path.exists())

    def test_tools_report_an_uninitialized_database(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "empty.sqlite3"
            db_path.touch()
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(db_path),
                },
            )
            registry = create_default_registry()

            search = asyncio.run(registry.run("library.asset.search", {}, context))
            update = asyncio.run(
                registry.run(
                    "library.asset.tags.update",
                    {"asset_id": "asset_missing", "add": ["tag"]},
                    context,
                )
            )

            self.assertEqual(search.error.code, "asset_search_failed")
            self.assertEqual(search.error.category.value, "database")
            self.assertEqual(update.error.code, "asset_tags_update_failed")
            self.assertEqual(update.error.category.value, "database")

    def test_tool_contracts_update_and_search_assets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "mediagent.sqlite3"
            asset_id = self._asset(db_path, root / "library" / "sample.jpg", title="Sample")
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(root),
                    "MEDIAGENT_DB_PATH": str(db_path),
                },
            )
            registry = create_default_registry()

            updated = asyncio.run(
                registry.run(
                    "library.asset.tags.update",
                    {"asset_id": asset_id, "add": ["manual"]},
                    context,
                )
            )
            found = asyncio.run(
                registry.run(
                    "library.asset.search",
                    {"terms": ["manual"], "limit": 10},
                    context,
                )
            )

            self.assertTrue(updated.is_success)
            self.assertEqual(updated.data["tags_added"], ["manual"])
            self.assertTrue(found.is_success)
            self.assertEqual(found.data["assets"][0]["asset_id"], asset_id)

    @staticmethod
    def _asset(
        db_path: Path,
        path: Path,
        *,
        platform: str = "local",
        remote_id: str | None = None,
        title: str | None = None,
        author: str | None = None,
        description: str | None = None,
    ) -> str:
        db.initialize_database(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"content:{path}".encode())
        identity = remote_id or path.stem
        metadata = {key: value for key, value in {"title": title, "description": description}.items() if value}
        db.upsert_media_item(
            db_path,
            {
                "platform": platform,
                "remote_id": identity,
                "source_url": f"https://example.invalid/{platform}/{identity}",
                "author_name": author,
                "media_type": "photo",
                "status": "downloaded",
                "metadata": metadata,
            },
        )
        checksum, size = library_content.sha256_checksum(path)
        record = db.upsert_media_file(
            db_path,
            platform=platform,
            remote_id=identity,
            file_key="main",
            remote_url=f"https://cdn.example.invalid/{platform}/{identity}",
            local_path=str(path),
            mime_type="image/jpeg",
            size_bytes=size,
            checksum=checksum,
            status="downloaded",
            library_relative_path=str(path.relative_to(path.parents[1])),
            file_health="healthy",
        )
        return str(library_content.adopt_media_file(db_path, file_id=int(record["id"]))["asset_id"])


if __name__ == "__main__":
    unittest.main()
