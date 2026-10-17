from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mediagent.core import comic_sync_state as state, db


class ComicSyncStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite"
        self.scope = dict(
            provider="jmcomic", account_key="account-hash", album_id="100"
        )
        self.now = datetime(2026, 10, 16, tzinfo=UTC)
        db.initialize_database(self.path)

    def save(self, ids=("1", "2"), **kwargs):
        return state.save_manifest(
            self.path,
            **self.scope,
            latest_photo_id=ids[-1] if ids else None,
            episodes=[dict(id=item, fingerprint="fp-" + item) for item in ids],
            now=self.now,
            **kwargs,
        )

    def read(self):
        return state.read_album(self.path, **self.scope)

    def claim(self, chapter="1", **kwargs):
        return state.claim_chapter(
            self.path,
            **self.scope,
            chapter_id=chapter,
            owner="worker",
            now=self.now,
            **kwargs,
        )

    def finish(self, lease, chapter="1", status="succeeded", **kwargs):
        return state.finish_chapter(
            self.path,
            **self.scope,
            chapter_id=chapter,
            token=lease["token"],
            generation=lease["generation"],
            status=status,
            now=self.now,
            **kwargs,
        )

    def test_observation_never_claims_completion_or_creates_missing_database(self):
        missing = self.path.parent / "missing.sqlite"
        self.assertIsNone(state.read_album(missing, **self.scope))
        self.assertFalse(missing.exists())
        album = self.save()
        self.assertIsNone(album["completed_generation"])
        self.assertEqual(
            ["pending", "pending"], [c["status"] for c in album["chapters"]]
        )
        self.assertTrue(self.finish(self.claim()))
        self.assertIsNone(self.read()["completed_generation"])
        self.assertTrue(self.finish(self.claim("2"), "2"))
        self.assertEqual(1, self.read()["completed_generation"])

    def test_identical_manifest_preserves_failed_and_running_work(self):
        self.save()
        lease = self.claim()
        self.assertTrue(
            self.finish(lease, status="failed", error_code="network_timeout")
        )
        running = self.claim("2")
        album = self.save()
        first, second = album["chapters"]
        self.assertEqual("failed", first["status"])
        self.assertEqual(1, first["attempt_count"])
        self.assertEqual("2026-10-16T00:01:00+00:00", first["next_attempt_at"])
        self.assertEqual(running["token"], second["lease_token"])
        self.assertIsNone(self.claim())
        self.now += timedelta(seconds=60)
        self.assertTrue(self.finish(self.claim()))

    def test_append_preserves_done_and_invalidates_stale_generation(self):
        self.save()
        self.finish(self.claim())
        stale = self.claim("2")
        album = self.save(("1", "2", "3"))
        self.assertEqual(2, album["generation"])
        self.assertEqual(
            ["succeeded", "pending", "pending"],
            [c["status"] for c in album["chapters"]],
        )
        self.assertFalse(self.finish(stale, "2"))
        self.assertTrue(self.finish(self.claim("2"), "2"))

    def test_fingerprint_change_only_reopens_affected_chapter(self):
        self.save()
        state.adopt_chapters(
            self.path,
            **self.scope,
            states={"1": "succeeded", "2": "succeeded"},
            now=self.now,
        )
        album = state.save_manifest(
            self.path,
            **self.scope,
            latest_photo_id="2",
            episodes=[
                dict(id="1", fingerprint="fp-1"),
                dict(id="2", fingerprint="changed"),
            ],
            now=self.now,
        )
        self.assertEqual(
            ["succeeded", "pending"], [c["status"] for c in album["chapters"]]
        )
        self.assertNotEqual(album["generation"], album["completed_generation"])

    def test_expired_lease_and_wrong_token_cannot_finish_or_renew(self):
        self.save()
        lease = self.claim(lease_seconds=10)
        self.assertEqual(self.now.isoformat(), self.read()["last_work_at"])
        self.assertIsNone(self.claim())
        bad = dict(lease, token="wrong")
        self.assertFalse(self.finish(bad))
        self.now += timedelta(seconds=11)
        self.assertFalse(self.finish(lease))
        self.assertFalse(
            state.renew_chapter(
                self.path,
                **self.scope,
                chapter_id="1",
                token=lease["token"],
                generation=lease["generation"],
                now=self.now,
            )
        )
        replacement = self.claim()
        self.assertNotEqual(lease["token"], replacement["token"])
        self.assertTrue(self.finish(replacement))
        self.assertFalse(self.finish(lease))

    def test_renew_lease_keeps_work_owned_until_new_expiry(self):
        self.save()
        lease = self.claim(lease_seconds=10)
        self.now += timedelta(seconds=9)
        self.assertTrue(
            state.renew_chapter(
                self.path,
                **self.scope,
                chapter_id="1",
                token=lease["token"],
                generation=lease["generation"],
                lease_seconds=100,
                now=self.now,
            )
        )
        self.now += timedelta(seconds=2)
        self.assertIsNone(self.claim())
        self.assertTrue(self.finish(lease))

    def test_full_revalidation_resumes_and_adoption_cannot_bypass_it(self):
        self.save()
        state.adopt_chapters(
            self.path,
            **self.scope,
            states={"1": "succeeded", "2": "succeeded"},
            now=self.now,
        )
        self.assertIsNone(self.read()["chapters"][0]["last_verified_at"])
        album = self.save(revalidate_all=True)
        self.assertEqual(2, album["generation"])
        state.adopt_chapters(
            self.path,
            **self.scope,
            states={"1": "succeeded", "2": "succeeded"},
            now=self.now,
        )
        self.assertEqual(
            ["pending", "pending"], [c["status"] for c in self.read()["chapters"]]
        )
        self.finish(self.claim())
        album = self.save(revalidate_all=True)
        self.assertEqual(2, album["generation"])
        self.assertTrue(album["revalidation_active"])
        self.assertEqual(
            ["succeeded", "pending"], [c["status"] for c in album["chapters"]]
        )
        self.finish(self.claim("2"), "2")
        self.assertFalse(self.read()["revalidation_active"])
        self.assertEqual(2, self.read()["completed_generation"])
        self.assertEqual(3, self.save(revalidate_all=True)["generation"])

    def test_paused_is_not_complete_and_suppressed_is_not_revived(self):
        self.save()
        state.adopt_chapters(
            self.path,
            **self.scope,
            states={"1": "paused", "2": "suppressed"},
            now=self.now,
        )
        self.assertIsNone(self.read()["completed_generation"])
        self.assertIsNone(self.claim("2"))
        self.assertFalse(
            state.mark_chapter_pending(
                self.path, **self.scope, chapter_id="2", now=self.now
            )
        )
        self.finish(self.claim())
        self.assertEqual(1, self.read()["completed_generation"])
        self.save(revalidate_all=True)
        self.assertEqual("suppressed", self.read()["chapters"][1]["status"])
        album = state.save_manifest(
            self.path,
            **self.scope,
            latest_photo_id="2",
            episodes=[dict(id="1"), dict(id="2", fingerprint="changed")],
            now=self.now,
        )
        self.assertEqual("suppressed", album["chapters"][1]["status"])

    def test_explicit_missing_repair_reopens_completion_without_remote_audit_credit(
        self,
    ):
        self.save(("1",))
        self.finish(self.claim())
        self.now += timedelta(hours=1)
        self.assertTrue(
            state.mark_chapter_pending(
                self.path, **self.scope, chapter_id="1", reason="missing", now=self.now
            )
        )
        self.assertIsNone(self.read()["completed_generation"])
        self.finish(self.claim(), verified_remote=False)
        chapter = self.read()["chapters"][0]
        self.assertEqual("2026-10-16T00:00:00+00:00", chapter["last_verified_at"])
        self.assertEqual(self.now.isoformat(), chapter["last_local_verified_at"])

    def test_read_legacy_and_migration_is_additive_and_idempotent(self):
        with closing(db.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE comic_sync_chapters")
            connection.execute("DROP TABLE comic_sync_albums")
            connection.execute(
                "UPDATE schema_meta SET value='15' WHERE key='schema_version'"
            )
            connection.execute("""INSERT INTO sync_cursors
                (platform,cursor_name,cursor_value,updated_at)
                VALUES ('jmcomic','favorites','observed-only','2026-10-16')""")
            connection.execute("""INSERT INTO media_items
                (platform,remote_id,media_type,status,created_at,updated_at)
                VALUES ('jmcomic','photo:1','comic','discovered','2026-10-16','2026-10-16')""")
            connection.execute("""INSERT INTO media_files
                (media_item_id,file_key,status,local_path)
                VALUES (1,'page:1','discovered','/synthetic/library/page.jpg')""")
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "media_items",
                    "media_files",
                    "library_entries",
                    "sync_cursors",
                )
            }
        self.assertIsNone(self.read())
        result = db.initialize_database(self.path)
        self.assertEqual("15", result["previous_schema_version"])
        self.assertEqual("16", result["schema_version"])
        self.assertFalse(db.initialize_database(self.path)["migrated"])
        self.assertIsNone(self.read())
        with closing(db.connect(self.path)) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
        self.assertEqual(before, after)

    def test_audit_cursor_and_scopes_are_independent(self):
        self.save()
        state.set_audit_cursor(self.path, **self.scope, cursor=7, now=self.now)
        self.assertEqual(7, self.read()["audit_cursor"])
        self.assertEqual(self.now.isoformat(), self.read()["last_audit_at"])
        other = dict(self.scope, account_key="other")
        self.assertIsNone(state.read_album(self.path, **other))
        self.assertIsNone(
            state.claim_chapter(self.path, **other, chapter_id="1", owner="other")
        )

    def test_rejects_transport_context_and_duplicate_chapters(self):
        for bad in ("https://example.com/token", "password secret", "../escape", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                state.save_manifest(
                    self.path,
                    **self.scope,
                    latest_photo_id="1",
                    episodes=[dict(id=bad)],
                )
        with self.assertRaises(ValueError):
            self.save(("1", "1"))

    def test_collection_revalidation_does_not_reopen_finished_album_in_active_pass(
        self,
    ):
        self.save(("1",))
        other = dict(self.scope, album_id="200")
        state.save_manifest(
            self.path,
            **other,
            latest_photo_id="2",
            episodes=[dict(id="2")],
            now=self.now,
        )
        state.adopt_chapters(
            self.path, **self.scope, states={"1": "succeeded"}, now=self.now
        )
        state.adopt_chapters(
            self.path, **other, states={"2": "succeeded"}, now=self.now
        )
        selection = dict(
            provider=self.scope["provider"],
            account_key=self.scope["account_key"],
            album_ids=["100", "200"],
        )
        self.assertTrue(
            state.request_revalidation(self.path, **selection, now=self.now)
        )
        self.assertEqual(2, self.read()["generation"])
        self.assertTrue(self.finish(self.claim()))
        completed = self.read()
        self.assertFalse(completed["revalidation_active"])
        self.assertFalse(
            state.request_revalidation(self.path, **selection, now=self.now)
        )
        self.assertEqual(completed, self.read())
        waiting = state.read_album(self.path, **other)
        self.assertEqual(2, waiting["generation"])
        self.assertTrue(waiting["revalidation_active"])
        self.assertEqual("pending", waiting["chapters"][0]["status"])
        lease = state.claim_chapter(
            self.path, **other, chapter_id="2", owner="worker", now=self.now
        )
        self.assertTrue(
            state.finish_chapter(
                self.path,
                **other,
                chapter_id="2",
                token=lease["token"],
                generation=lease["generation"],
                status="succeeded",
                now=self.now,
            )
        )
        self.assertTrue(
            state.request_revalidation(self.path, **selection, now=self.now)
        )
        self.assertEqual(3, self.read()["generation"])

    def test_unvisited_collection_album_keeps_revalidation_intent_and_rejects_adoption(
        self,
    ):
        selection = dict(
            provider=self.scope["provider"],
            account_key=self.scope["account_key"],
            album_ids=["100", "200"],
        )
        self.assertTrue(
            state.request_revalidation(self.path, **selection, now=self.now)
        )
        placeholder = self.read()
        self.assertTrue(placeholder["revalidation_active"])
        self.assertEqual([], placeholder["chapters"])
        self.assertIsNone(placeholder["completed_generation"])
        # Even an unrelated legacy adoption attempt must not mistake an
        # unobserved manifest with no chapters for a completed empty album.
        state.adopt_chapters(self.path, **self.scope, states={}, now=self.now)
        self.assertTrue(self.read()["revalidation_active"])
        self.assertFalse(
            state.request_revalidation(self.path, **selection, now=self.now)
        )
        album = self.save(("1",))
        self.assertTrue(album["revalidation_active"])
        self.assertEqual(0, album["chapters"][0]["adoption_pending"])
        state.adopt_chapters(
            self.path, **self.scope, states={"1": "succeeded"}, now=self.now
        )
        self.assertEqual("pending", self.read()["chapters"][0]["status"])
        self.assertTrue(self.finish(self.claim()))
        waiting = state.read_album(self.path, **dict(self.scope, album_id="200"))
        self.assertTrue(waiting["revalidation_active"])
        self.assertIsNone(waiting["completed_generation"])

    def test_collection_revalidation_fences_leases_and_preserves_suppression(self):
        self.save()
        stale = self.claim()
        state.adopt_chapters(
            self.path, **self.scope, states={"2": "suppressed"}, now=self.now
        )
        self.assertTrue(
            state.request_revalidation(
                self.path,
                provider=self.scope["provider"],
                account_key=self.scope["account_key"],
                album_ids=["100", "100"],
                now=self.now,
            )
        )
        album = self.read()
        self.assertEqual(2, album["generation"])
        self.assertEqual(
            ["pending", "suppressed"], [c["status"] for c in album["chapters"]]
        )
        self.assertTrue(all(c["lease_token"] is None for c in album["chapters"]))
        self.assertFalse(self.finish(stale))
        self.assertFalse(
            state.request_revalidation(
                self.path,
                provider=self.scope["provider"],
                account_key=self.scope["account_key"],
                album_ids=[],
                now=self.now,
            )
        )
