import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core import db


class ComicCollectionStateTests(unittest.TestCase):
    def test_collection_scope_alias_registers_resolves_and_renames_by_remote_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            db.initialize_database(db_path)
            first = db.register_collection_scope_alias(
                db_path,
                provider="jmcomic",
                account_key="favorites:test",
                scope_kind="favorite_folder",
                scope_name="Watch Later",
                scope_name_key="watch later",
                remote_id="123",
                canonical_url="https://18comic.vip/user/test/favorite/albums?folder=123",
            )
            db.register_collection_scope_alias(
                db_path,
                provider="jmcomic",
                account_key="favorites:test",
                scope_kind="favorite_folder",
                scope_name="Read Next",
                scope_name_key="read next",
                remote_id="123",
            )

            self.assertEqual(first["remote_id"], "123")
            self.assertIsNone(
                db.get_collection_scope_alias(
                    db_path,
                    provider="jmcomic",
                    account_key="favorites:test",
                    scope_kind="favorite_folder",
                    scope_name_key="watch later",
                )
            )
            renamed = db.get_collection_scope_alias(
                db_path,
                provider="jmcomic",
                account_key="favorites:test",
                scope_kind="favorite_folder",
                scope_name_key="read next",
            )
            self.assertEqual(renamed["remote_id"], "123")

    def test_collection_scope_alias_rejects_name_reassignment_without_replace(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            db.initialize_database(db_path)
            kwargs = {
                "provider": "jmcomic",
                "account_key": "favorites:test",
                "scope_kind": "favorite_folder",
                "scope_name": "Watch Later",
                "scope_name_key": "watch later",
            }
            db.register_collection_scope_alias(db_path, remote_id="123", **kwargs)
            with self.assertRaisesRegex(ValueError, "already registered"):
                db.register_collection_scope_alias(db_path, remote_id="456", **kwargs)
            db.register_collection_scope_alias(db_path, remote_id="456", replace=True, **kwargs)
            aliases = db.list_collection_scope_aliases(
                db_path,
                provider="jmcomic",
                account_key="favorites:test",
                scope_kind="favorite_folder",
            )
            self.assertEqual([(row["name"], row["remote_id"]) for row in aliases], [("Watch Later", "456")])

    def test_complete_snapshot_adds_retains_and_deactivates_memberships(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            db.initialize_database(db_path)

            first = db.commit_collection_snapshot(
                db_path,
                provider="jmcomic",
                collection_key="favorites",
                targets=[
                    {"target_type": "album", "target_id": "100"},
                    {"target_type": "album", "target_id": "200"},
                ],
            )
            second = db.commit_collection_snapshot(
                db_path,
                provider="jmcomic",
                collection_key="favorites",
                targets=[
                    {"target_type": "album", "target_id": "200"},
                    {"target_type": "album", "target_id": "300"},
                ],
            )

            self.assertEqual(first["added"], 2)
            self.assertEqual(second["added"], 1)
            self.assertEqual(second["retained"], 1)
            self.assertEqual(second["removed"], 1)
            active = db.list_collection_memberships(
                db_path,
                provider="jmcomic",
                collection_key="favorites",
            )
            inactive = db.list_collection_memberships(
                db_path,
                provider="jmcomic",
                collection_key="favorites",
                active=False,
            )
            self.assertEqual([row["target_id"] for row in active], ["200", "300"])
            self.assertEqual([row["target_id"] for row in inactive], ["100"])

    def test_empty_complete_snapshot_deactivates_previous_memberships(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            db.initialize_database(db_path)
            db.commit_collection_snapshot(
                db_path,
                provider="nhentai",
                collection_key="favorites",
                targets=[{"target_type": "gallery", "target_id": "10"}],
            )

            result = db.commit_collection_snapshot(
                db_path,
                provider="nhentai",
                collection_key="favorites",
                targets=[],
            )

            self.assertEqual(result["removed"], 1)
            self.assertEqual(
                db.list_collection_memberships(
                    db_path,
                    provider="nhentai",
                    collection_key="favorites",
                ),
                [],
            )

    def test_invalid_snapshot_does_not_change_previous_memberships(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            db.initialize_database(db_path)
            db.commit_collection_snapshot(
                db_path,
                provider="jmcomic",
                collection_key="favorites",
                targets=[{"target_type": "album", "target_id": "100"}],
            )

            with self.assertRaises(ValueError):
                db.commit_collection_snapshot(
                    db_path,
                    provider="jmcomic",
                    collection_key="favorites",
                    targets=[{"target_type": "album", "target_id": ""}],
                )

            active = db.list_collection_memberships(
                db_path,
                provider="jmcomic",
                collection_key="favorites",
            )
            self.assertEqual([row["target_id"] for row in active], ["100"])


if __name__ == "__main__":
    unittest.main()
