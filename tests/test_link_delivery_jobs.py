"""Offline durable-delivery and schema-14 recovery regression coverage."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mediagent.core import db, link_delivery_jobs as jobs


class LinkDeliveryJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        db.initialize_database(self.path)
        self.now = datetime(2026, 10, 16, tzinfo=UTC)

    def link(self, suffix="one", **values):
        data = {
            "ingest_platform": "telegram",
            "original_url": "https://example.com/" + suffix,
            "normalized_url": "https://example.com/" + suffix,
            "source_message_id": "1",
        }
        data.update(values)
        return db.upsert_link(self.path, data)

    def test_enqueue_is_idempotent_and_success_is_not_revived(self):
        link = self.link()
        first = jobs.enqueue(self.path, link["id"], now=self.now)
        self.assertEqual(first, jobs.enqueue(self.path, link["id"], now=self.now))
        leased = jobs.claim_ready(self.path, "worker", now=self.now)[0]
        jobs.finish(
            self.path, leased["id"], "worker", leased["lease_token"], now=self.now
        )
        self.assertEqual("succeeded", jobs.enqueue(self.path, link["id"])["status"])
        self.assertEqual([], jobs.claim_ready(self.path, "worker", now=self.now))

    def test_explicit_completed_repair_preserves_binding_and_claims_only_selection(
        self,
    ):
        target = self.link("target")
        unrelated = self.link("unrelated")
        db.upsert_media_item(
            self.path,
            {
                "platform": "fixture",
                "remote_id": "bound",
                "media_type": "video",
                "status": "downloaded",
            },
        )
        jobs.enqueue(self.path, target["id"], now=self.now)
        jobs.bind_media(self.path, target["id"], [("fixture", "bound")])
        claimed = jobs.claim_ready(self.path, "worker", now=self.now)[0]
        jobs.finish(
            self.path, claimed["id"], "worker", claimed["lease_token"], now=self.now
        )
        jobs.enqueue(self.path, unrelated["id"], now=self.now)
        repair = jobs.enqueue(self.path, target["id"], force=True, now=self.now)
        self.assertEqual("pending", repair["status"])
        self.assertEqual(1, repair["attempt_count"])
        self.assertEqual(3, repair["max_attempts"] - repair["attempt_count"])
        self.assertEqual(
            [], jobs.claim_ready(self.path, "repair", link_ids=[], now=self.now)
        )
        selected = jobs.claim_ready(
            self.path, "repair", link_ids=[target["id"]], now=self.now
        )
        self.assertEqual([target["id"]], [job["link_id"] for job in selected])
        self.assertEqual("pending", jobs.enqueue(self.path, unrelated["id"])["status"])
        with db.connect(self.path) as con:
            self.assertEqual(
                1,
                con.execute(
                    "SELECT COUNT(*) FROM link_delivery_media WHERE link_id=?",
                    (target["id"],),
                ).fetchone()[0],
            )

    def test_retry_backoff_exhaustion_and_explicit_retry_preserve_history(self):
        link = self.link()
        jobs.enqueue(self.path, link["id"], now=self.now)
        for attempt in range(3):
            leased = jobs.claim_ready(self.path, "worker", now=self.now)[0]
            result = jobs.finish(
                self.path,
                leased["id"],
                "worker",
                leased["lease_token"],
                outcome="retry",
                error_code="network_error",
                now=self.now,
            )
            self.assertEqual(attempt + 1, result["attempt_count"])
            self.assertEqual([], jobs.claim_ready(self.path, "worker", now=self.now))
            self.now += timedelta(hours=1)
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, jobs.requeue(self.path, [link["id"]], now=self.now))
        leased = jobs.claim_ready(self.path, "worker", now=self.now)[0]
        self.assertEqual(4, leased["attempt_count"])
        self.assertEqual(6, leased["max_attempts"])

    def test_expired_and_replaced_lease_cannot_finish(self):
        link = self.link()
        jobs.enqueue(self.path, link["id"], now=self.now)
        old = jobs.claim_ready(self.path, "old", lease_seconds=1, now=self.now)[0]
        later = self.now + timedelta(seconds=1)
        self.assertIsNone(
            jobs.finish(self.path, old["id"], "old", old["lease_token"], now=later)
        )
        new = jobs.claim_ready(self.path, "new", now=later)[0]
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        self.assertIsNone(
            jobs.finish(self.path, old["id"], "old", old["lease_token"], now=later)
        )
        self.assertEqual(
            "succeeded",
            jobs.finish(self.path, new["id"], "new", new["lease_token"], now=later)[
                "status"
            ],
        )

    def test_crashed_final_attempt_is_failed_not_stuck_running(self):
        link = self.link()
        jobs.enqueue(self.path, link["id"], now=self.now)
        for _ in range(3):
            self.assertEqual(
                1,
                len(
                    jobs.claim_ready(self.path, "worker", lease_seconds=1, now=self.now)
                ),
            )
            self.now += timedelta(seconds=2)
        self.assertEqual([], jobs.claim_ready(self.path, "worker", now=self.now))
        self.assertEqual(1, jobs.summary(self.path)["failed"])
        self.assertEqual(0, jobs.summary(self.path)["running"])

    def test_renew_only_live_owned_lease_and_retry_after_is_bounded(self):
        link = self.link()
        jobs.enqueue(self.path, link["id"], now=self.now)
        job = jobs.claim_ready(self.path, "worker", lease_seconds=2, now=self.now)[0]
        later = self.now + timedelta(seconds=1)
        self.assertFalse(
            jobs.renew(self.path, job["id"], "other", job["lease_token"], now=later)
        )
        self.assertTrue(
            jobs.renew(self.path, job["id"], "worker", job["lease_token"], now=later)
        )
        result = jobs.finish(
            self.path,
            job["id"],
            "worker",
            job["lease_token"],
            outcome="retry",
            retry_after_seconds=999999,
            now=later,
        )
        self.assertEqual(
            (later + timedelta(days=1)).isoformat(), result["next_attempt_at"]
        )
        self.assertFalse(
            jobs.renew(self.path, job["id"], "worker", job["lease_token"], now=later)
        )
        later += timedelta(days=2)
        job = jobs.claim_ready(self.path, "worker", lease_seconds=1, now=later)[0]
        self.assertFalse(
            jobs.renew(
                self.path,
                job["id"],
                "worker",
                job["lease_token"],
                now=later + timedelta(seconds=1),
            )
        )

    def test_missing_and_legacy_database_backlog_is_empty_without_creation(self):
        missing = Path(self.temp.name) / "absent.sqlite3"
        self.assertEqual([], jobs.list_jobs(missing))
        self.assertFalse(jobs.is_leased(missing, 1))
        self.assertEqual(0, sum(jobs.summary(missing).values()))
        self.assertFalse(missing.exists())
        with db.connect(self.path) as con:
            con.execute("DROP TABLE link_delivery_jobs")
        self.assertEqual([], jobs.list_jobs(self.path))
        self.assertFalse(jobs.is_leased(self.path, 1))
        self.assertEqual(0, sum(jobs.summary(self.path).values()))

    def test_auth_blocks_require_explicit_opt_in(self):
        failure = self.link("failed", status="failed", retryable=False)
        jobs.enqueue(self.path, failure["id"], now=self.now)
        link = self.link(
            status="deferred",
            skip_reason="requires_auth",
            attempt_count=2,
            resolution={"details": {"error_code": "instagram_session_missing"}},
        )
        job = jobs.enqueue(self.path, link["id"], now=self.now)
        self.assertEqual("blocked", job["status"])
        self.assertEqual(0, jobs.requeue(self.path, [link["id"]], now=self.now))
        self.assertEqual(
            1, jobs.requeue(self.path, [link["id"]], blocked_auth=True, now=self.now)
        )
        self.assertEqual("failed", jobs.enqueue(self.path, failure["id"])["status"])
        self.assertEqual(
            3, jobs.claim_ready(self.path, "worker", now=self.now)[0]["attempt_count"]
        )

    def test_active_legacy_lease_is_respected(self):
        link = self.link(
            lease_owner="legacy",
            lease_expires_at=(self.now + timedelta(minutes=1)).isoformat(),
        )
        jobs.enqueue(self.path, link["id"], now=self.now)
        self.assertEqual([], jobs.claim_ready(self.path, "worker", now=self.now))
        self.assertEqual(
            1,
            len(
                jobs.claim_ready(
                    self.path, "worker", now=self.now + timedelta(minutes=2)
                )
            ),
        )

    def test_legacy_workers_cannot_claim_active_delivery_or_auth_delivery(self):
        plain = self.link()
        jobs.enqueue(self.path, plain["id"], now=self.now)
        leased = jobs.claim_ready(self.path, "delivery", now=self.now)[0]
        self.assertTrue(jobs.is_leased(self.path, plain["id"], now=self.now))
        self.assertTrue(
            jobs.owns(
                self.path, leased["id"], "delivery", leased["lease_token"], now=self.now
            )
        )
        self.assertFalse(
            jobs.owns(
                self.path, leased["id"], "other", leased["lease_token"], now=self.now
            )
        )
        self.assertFalse(
            jobs.owns(self.path, leased["id"], "delivery", "stale", now=self.now)
        )
        self.assertEqual(
            [],
            db.claim_links(
                self.path, lease_owner="legacy", now=self.now.isoformat(), limit=1
            ),
        )
        self.assertEqual(
            [], db.list_ready_links(self.path, now=self.now.isoformat(), limit=1)
        )
        # A resolver can persist an authentication result while full delivery
        # retains its lease, so the old auth retry worker must also stay out.
        with db.connect(self.path) as con:
            con.execute(
                "UPDATE link_queue SET status='skipped',skip_reason='requires_auth' WHERE id=?",
                (plain["id"],),
            )
        self.assertEqual(
            [],
            db.claim_auth_skipped_links(
                self.path, lease_owner="legacy", now=self.now.isoformat(), limit=1
            ),
        )
        self.assertEqual(
            [], db.list_auth_skipped_links(self.path, now=self.now.isoformat(), limit=1)
        )
        later = self.now + timedelta(seconds=901)
        self.assertFalse(jobs.is_leased(self.path, plain["id"], now=later))
        self.assertFalse(
            jobs.owns(
                self.path, leased["id"], "delivery", leased["lease_token"], now=later
            )
        )
        self.assertEqual(
            [],
            db.claim_auth_skipped_links(
                self.path, lease_owner="legacy", now=later.isoformat()
            ),
        )
        self.assertEqual(1, len(jobs.claim_ready(self.path, "delivery", now=later)))
        self.assertIsNone(
            jobs.finish(
                self.path, leased["id"], "delivery", leased["lease_token"], now=later
            )
        )

    def test_delivery_ownership_excludes_legacy_workers_in_every_job_state(self):
        for index, status in enumerate(
            ("pending", "deferred", "blocked", "failed", "succeeded", "running")
        ):
            link = self.link(str(index))
            jobs.enqueue(self.path, link["id"], now=self.now)
            with db.connect(self.path) as con:
                con.execute(
                    """UPDATE link_delivery_jobs SET status=?,lease_expires_at=?
                    WHERE link_id=?""",
                    (status, self.now.isoformat(), link["id"]),
                )
        self.assertEqual([], db.list_ready_links(self.path, now=self.now.isoformat()))
        self.assertEqual(
            [],
            db.claim_links(self.path, lease_owner="legacy", now=self.now.isoformat()),
        )
        with db.connect(self.path) as con:
            con.execute(
                "UPDATE link_queue SET status='skipped',skip_reason='requires_auth'"
            )
        self.assertEqual(
            [], db.list_auth_skipped_links(self.path, now=self.now.isoformat())
        )
        self.assertEqual(
            [],
            db.claim_auth_skipped_links(
                self.path, lease_owner="legacy", now=self.now.isoformat()
            ),
        )

    def test_legacy_claims_work_without_delivery_table(self):
        plain = self.link()
        with db.connect(self.path) as con:
            con.execute("DROP TABLE link_delivery_jobs")
        self.assertEqual(
            1, len(db.list_ready_links(self.path, now=self.now.isoformat(), limit=1))
        )
        self.assertEqual(
            1,
            len(
                db.claim_links(
                    self.path, lease_owner="legacy", now=self.now.isoformat(), limit=1
                )
            ),
        )
        with db.connect(self.path) as con:
            con.execute(
                "UPDATE link_queue SET status='skipped',skip_reason='requires_auth',lease_owner=NULL WHERE id=?",
                (plain["id"],),
            )
        self.assertEqual(
            1,
            len(
                db.list_auth_skipped_links(self.path, now=self.now.isoformat(), limit=1)
            ),
        )
        self.assertEqual(
            1,
            len(
                db.claim_auth_skipped_links(
                    self.path, lease_owner="legacy", now=self.now.isoformat(), limit=1
                )
            ),
        )

    def test_schema14_sanitized_88_and_stories_upgrade_idempotently(self):
        media_path = Path(self.temp.name) / "fixture.bin"
        media_path.write_bytes(b"synthetic preserved media")
        checksum = hashlib.sha256(media_path.read_bytes()).hexdigest()
        media = db.upsert_media_item(
            self.path,
            {
                "platform": "fixture",
                "remote_id": "keep",
                "media_type": "video",
                "status": "downloaded",
                "metadata": {"source_provenance": [{"message_id": "old"}]},
            },
        )
        with db.connect(self.path) as con:
            con.execute(
                """INSERT INTO media_files(media_item_id,file_key,local_path,checksum,status)
                           VALUES (?,?,?,?,?)""",
                (media["id"], "video:0", str(media_path), checksum, "downloaded"),
            )
            original_media = [
                tuple(row) for row in con.execute("SELECT * FROM media_items")
            ]
            original_files = [
                tuple(row) for row in con.execute("SELECT * FROM media_files")
            ]
        bad = self.link(
            "regional",
            normalized_url="https://jp.pornhub.com/view_video.php?viewkey=publicfixture",
            status="deferred",
            skip_reason="resolver_error",
            attempt_count=2,
            max_attempts=3,
            next_attempt_at="2099-01-01T00:00:00+00:00",
            resolution={
                "resolver": "generic_html_media",
                "details": {"status_code": 404},
            },
        )
        story = self.link(
            "story",
            normalized_url="https://www.instagram.com/stories/test/123/",
            status="deferred",
            skip_reason="resolver_error",
            attempt_count=2,
            resolution={"resolver": "instagram_media_link"},
        )
        generic = self.link(
            "gone",
            status="deferred",
            skip_reason="resolver_error",
            attempt_count=2,
            resolution={
                "resolver": "generic_html_media",
                "details": {"status_code": 404},
            },
        )
        with db.connect(self.path) as con:
            con.execute("UPDATE link_queue SET id=88 WHERE id=?", (bad["id"],))
            con.execute("UPDATE schema_meta SET value='14' WHERE key='schema_version'")
        self.assertEqual("16", db.initialize_database(self.path)["schema_version"])
        with db.connect(self.path) as con:
            before = [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM link_delivery_jobs ORDER BY link_id"
                )
            ]
            rows = {
                row["id"]: dict(row) for row in con.execute("SELECT * FROM link_queue")
            }
        self.assertEqual("queued", rows[88]["status"])
        self.assertEqual(2, rows[88]["attempt_count"])
        self.assertEqual(5, rows[88]["max_attempts"])
        self.assertEqual("skipped", rows[story["id"]]["status"])
        self.assertEqual(0, rows[generic["id"]]["retryable"])
        self.assertEqual(
            [88], [job["link_id"] for job in jobs.claim_ready(self.path, "worker")]
        )
        with db.connect(self.path) as con:
            after_claim = [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM link_delivery_jobs ORDER BY link_id"
                )
            ]
        db.initialize_database(self.path)
        with db.connect(self.path) as con:
            after = [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM link_delivery_jobs ORDER BY link_id"
                )
            ]
            self.assertEqual(
                original_media,
                [tuple(row) for row in con.execute("SELECT * FROM media_items")],
            )
            self.assertEqual(
                original_files,
                [tuple(row) for row in con.execute("SELECT * FROM media_files")],
            )
            self.assertEqual("ok", con.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual(checksum, hashlib.sha256(media_path.read_bytes()).hexdigest())
        self.assertEqual(after_claim, after)
        self.assertEqual(3, len(before))
        self.assertEqual(2, jobs.summary(self.path)["failed"])

    def test_old_reels_classify_timeout_rate_limit_auth_and_missing(self):
        records = []
        for index, code in enumerate(
            (
                "instagram_resolve_failed",
                "instagram_rate_limited",
                "instagram_session_invalid",
                "instagram_media_not_found",
            )
        ):
            records.append(
                self.link(
                    str(index),
                    normalized_url=f"https://www.instagram.com/reel/fixture{index}/",
                    status="deferred",
                    skip_reason="resolver_error",
                    attempt_count=1,
                    next_attempt_at="2099-01-01T00:00:00+00:00",
                    resolution={
                        "resolver": "instagram_media_link",
                        "details": {"error_code": code},
                    },
                )
            )
        db.initialize_database(self.path)
        self.assertEqual(
            ["deferred", "deferred", "blocked", "failed"],
            [jobs.enqueue(self.path, record["id"])["status"] for record in records],
        )
        self.assertEqual([], jobs.claim_ready(self.path, "worker", now=self.now))

    def test_misrouted_legacy_delivery_grants_exactly_three_additional_attempts(self):
        for attempts in (2, 100):
            with self.subTest(attempts=attempts):
                link = self.link(
                    str(attempts),
                    normalized_url=f"https://cn.pornhub.com/view_video.php?viewkey=fixture{attempts}",
                    status="deferred",
                    skip_reason="resolver_error",
                    attempt_count=attempts,
                    resolution={
                        "resolver": "generic_html_media",
                        "details": {"status_code": 404},
                    },
                )
                db.initialize_database(self.path)
                job = jobs.enqueue(self.path, link["id"])
                self.assertEqual(attempts, job["attempt_count"])
                self.assertEqual(3, job["max_attempts"] - job["attempt_count"])

    def test_migration_retains_schedule_exhaustion_and_cross_origin_provenance(self):
        deferred = self.link(
            "later",
            status="deferred",
            attempt_count=1,
            next_attempt_at="2099-01-01T00:00:00+00:00",
        )
        failed = self.link("failed", status="failed", attempt_count=3, retryable=False)
        other = self.link("other", ingest_platform="manual")
        self.link("other", ingest_platform="telegram", source_message_id="2")
        db.initialize_database(self.path)
        self.assertEqual(1, jobs.summary(self.path)["deferred"])
        self.assertEqual(
            [other["id"]],
            [
                job["link_id"]
                for job in jobs.claim_ready(self.path, "worker", now=self.now)
            ],
        )
        self.assertEqual("deferred", jobs.enqueue(self.path, deferred["id"])["status"])
        self.assertEqual("failed", jobs.enqueue(self.path, failed["id"])["status"])

    def test_delivered_binding_replays_provenance_without_new_download(self):
        link = self.link()
        db.upsert_media_item(
            self.path,
            {
                "platform": "fixture",
                "remote_id": "one",
                "media_type": "video",
                "status": "downloaded",
                "metadata": {"kept": True},
            },
        )
        jobs.enqueue(self.path, link["id"], now=self.now)
        self.assertEqual(
            1, jobs.bind_media(self.path, link["id"], [("fixture", "one")])
        )
        self.link(source_message_id="2")
        self.assertEqual(1, jobs.refresh_provenance(self.path, link["id"]))
        self.assertEqual(0, jobs.refresh_provenance(self.path, link["id"]))
        with db.connect(self.path) as con:
            row = con.execute("SELECT * FROM media_items").fetchone()
        metadata = json.loads(row["metadata_json"])
        self.assertEqual("downloaded", row["status"])
        self.assertTrue(metadata["kept"])
        self.assertEqual(
            {"1", "2"},
            {source["message_id"] for source in metadata["source_provenance"]},
        )

    def test_canonical_alias_binding_recovers_all_provenance_after_metadata_refresh(
        self,
    ):
        first = self.link("cn", source_message_id="first")
        second = self.link("jp", source_message_id="second")
        media = {
            "platform": "pornhub",
            "remote_id": "fixture",
            "media_type": "video",
            "status": "downloaded",
            "metadata": {"title": "synthetic"},
        }
        db.upsert_media_item(self.path, media)
        jobs.bind_media(self.path, first["id"], [("pornhub", "fixture")])
        # The next canonical alias resolution replaces provider metadata.
        db.upsert_media_item(
            self.path,
            {
                **media,
                "metadata": {
                    "title": "refreshed",
                    "source_provenance": [
                        {"platform": "historical", "message_id": "keep"}
                    ],
                },
            },
        )
        jobs.bind_media(self.path, second["id"], [("pornhub", "fixture")])
        with db.connect(self.path) as con:
            row = con.execute("SELECT * FROM media_items").fetchone()
        metadata = json.loads(row["metadata_json"])
        self.assertEqual("downloaded", row["status"])
        self.assertEqual("refreshed", metadata["title"])
        self.assertEqual(
            {"first", "second", "keep"},
            {source["message_id"] for source in metadata["source_provenance"]},
        )
        self.assertEqual(
            {first["id"], second["id"]},
            {
                source["link_id"]
                for source in metadata["source_provenance"]
                if "link_id" in source
            },
        )
        self.assertEqual(0, jobs.refresh_provenance(self.path, second["id"]))

    def test_retry_classification_uses_explicit_provider_then_safe_http(self):
        cases = [
            (
                {"resolver": "generic_html_media", "details": {"status_code": 404}},
                "resolver_error",
                False,
            ),
            (
                {"resolver": "generic_html_media", "details": {"status_code": 410}},
                "resolver_error",
                False,
            ),
            ({"details": {"status_code": 429}}, "resolver_error", True),
            ({"details": {"status_code": 503}}, "resolver_error", True),
            (
                {"details": {"status_code": 404, "retryable": True}},
                "resolver_error",
                True,
            ),
            ({}, "unsafe_redirect", False),
            ({}, "pornhub_url_unsupported", False),
            (
                {"details": {"error_code": "instagram_media_not_found"}},
                "resolver_error",
                False,
            ),
        ]
        for resolution, reason, expected in cases:
            with self.subTest(resolution=resolution, reason=reason):
                self.assertEqual(
                    expected,
                    db.link_resolution_retryable(
                        status="deferred", resolution=resolution, skip_reason=reason
                    ),
                )
