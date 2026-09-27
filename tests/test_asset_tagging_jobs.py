from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mediagent.core import asset_tagging_jobs, assets, db


class AssetTaggingJobTests(unittest.TestCase):
    def test_v13_migration_creates_jobs_without_asset_backfill(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            with db.connect(db_path) as connection:
                connection.execute("DROP TABLE asset_tagging_jobs")
                connection.execute("UPDATE schema_meta SET value = '13' WHERE key = 'schema_version'")

            with mock.patch("mediagent.core.assets.backfill") as backfill:
                result = db.initialize_database(db_path)

            self.assertEqual(result["previous_schema_version"], "13")
            self.assertEqual(result["schema_version"], "14")
            self.assertTrue(result["migrated"])
            self.assertFalse(result["asset_reconciled"])
            backfill.assert_not_called()
            with db.connect(db_path) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'asset_tagging_jobs'"
                ).fetchone()
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(asset_tagging_jobs)")
                }
            self.assertIsNotNone(table)
            self.assertTrue(
                {
                    "input_fingerprint",
                    "provider",
                    "model",
                    "prompt_version",
                    "lease_token",
                    "next_attempt_at",
                }.issubset(columns)
            )

    def test_enqueue_is_idempotent_and_changed_contract_resets_work(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path)
            first = self._enqueue(db_path, asset_id=asset_id)
            same = self._enqueue(db_path, asset_id=asset_id)
            claimed = asset_tagging_jobs.claim(db_path, owner="worker", lease_seconds=60)
            changed = asset_tagging_jobs.enqueue(
                db_path,
                asset_id=asset_id,
                input_fingerprint="sha256:changed",
                provider="openai_compatible",
                model="new-model",
                prompt_version="asset-tags-v2",
                max_attempts=7,
            )

            self.assertEqual(first["id"], same["id"])
            self.assertEqual(claimed[0]["input_fingerprint"], "sha256:input")
            self.assertEqual(claimed[0]["provider"], "openai_compatible")
            self.assertEqual(changed["id"], first["id"])
            self.assertEqual(changed["status"], "pending")
            self.assertEqual(changed["attempt_count"], 0)
            self.assertEqual(changed["max_attempts"], 7)
            self.assertEqual(changed["input_fingerprint"], "sha256:changed")
            self.assertEqual(changed["model"], "new-model")
            self.assertEqual(changed["prompt_version"], "asset-tags-v2")
            self.assertIsNone(changed["lease_token"])

    def test_claim_excludes_removed_and_purged_assets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            active = self._asset(db_path, suffix="active")
            removed = self._asset(db_path, suffix="removed", state="removed")
            purged = self._asset(db_path, suffix="purged", state="purged")
            for asset_id in (active, removed, purged):
                self._enqueue(db_path, asset_id=asset_id)

            claimed = asset_tagging_jobs.claim(db_path, owner="worker", limit=10)

            self.assertEqual([job["asset_id"] for job in claimed], [active])
            with db.connect(db_path) as connection:
                inactive_statuses = {
                    row["asset_id"]: row["status"]
                    for row in connection.execute(
                        "SELECT asset_id, status FROM asset_tagging_jobs WHERE asset_id IN (?, ?)",
                        (removed, purged),
                    )
                }
            self.assertEqual(inactive_statuses, {removed: "pending", purged: "pending"})

    def test_claim_asset_filter_only_leases_requested_batch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            old_asset = self._asset(db_path, suffix="old")
            current_asset = self._asset(db_path, suffix="current")
            self._enqueue(db_path, asset_id=old_asset)
            self._enqueue(db_path, asset_id=current_asset)

            none = asset_tagging_jobs.claim(
                db_path, owner="worker", asset_ids=[], limit=10
            )
            current = asset_tagging_jobs.claim(
                db_path, owner="worker", asset_ids=[current_asset], limit=10
            )
            remaining = asset_tagging_jobs.claim(db_path, owner="queue-worker", limit=10)

            self.assertEqual(none, [])
            self.assertEqual([job["asset_id"] for job in current], [current_asset])
            self.assertEqual([job["asset_id"] for job in remaining], [old_asset])

    def test_expired_lease_is_reclaimed_and_old_token_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path)
            self._enqueue(db_path, asset_id=asset_id)
            start = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
            first = asset_tagging_jobs.claim(
                db_path, owner="worker-a", lease_seconds=10, now=start
            )[0]
            self.assertEqual(asset_tagging_jobs.claim(db_path, owner="worker-b", now=start), [])

            reclaimed = asset_tagging_jobs.claim(
                db_path,
                owner="worker-b",
                lease_seconds=10,
                now=start + timedelta(seconds=11),
            )[0]

            self.assertNotEqual(reclaimed["lease_token"], first["lease_token"])
            self.assertEqual(reclaimed["attempt_count"], 2)
            with self.assertRaisesRegex(ValueError, "lease is no longer valid"):
                asset_tagging_jobs.complete(
                    db_path,
                    job_id=first["id"],
                    lease_token=first["lease_token"],
                    tags=["old worker"],
                )

    def test_ready_preflight_finalizes_crashed_last_attempt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path)
            self._enqueue(db_path, asset_id=asset_id, max_attempts=1)
            start = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
            claimed = asset_tagging_jobs.claim(
                db_path,
                owner="crashed-worker",
                lease_seconds=10,
                now=start,
            )[0]

            ready = asset_tagging_jobs.has_ready(
                db_path,
                now=start + timedelta(seconds=11),
            )

            self.assertFalse(ready)
            with db.connect(db_path) as connection:
                job = connection.execute(
                    "SELECT * FROM asset_tagging_jobs WHERE id = ?", (claimed["id"],)
                ).fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertFalse(job["retryable"])
            self.assertEqual(job["last_error_code"], "lease_expired")
            self.assertIsNone(job["lease_owner"])
            self.assertIsNone(job["lease_token"])

    def test_complete_merges_tags_and_state_in_one_transaction(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path, tags=["manual"])
            self._enqueue(db_path, asset_id=asset_id)
            claimed = asset_tagging_jobs.claim(db_path, owner="worker")[0]

            completed = asset_tagging_jobs.complete(
                db_path,
                job_id=claimed["id"],
                lease_token=claimed["lease_token"],
                tags=["Landscape", "manual", "Café"],
                provider="openai_compatible",
                model="runtime-model",
            )

            self.assertEqual(completed["job"]["status"], "succeeded")
            self.assertEqual(completed["job"]["provider"], "openai_compatible")
            self.assertEqual(completed["job"]["model"], "runtime-model")
            self.assertEqual(completed["asset"]["metadata"]["tags"], ["manual", "Landscape", "Café"])
            self.assertEqual(completed["asset"]["tags_added"], ["Landscape", "Café"])
            self.assertIsNone(completed["job"]["lease_token"])

    def test_complete_rolls_back_tag_write_when_job_transition_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path, tags=["manual"])
            self._enqueue(db_path, asset_id=asset_id)
            claimed = asset_tagging_jobs.claim(db_path, owner="worker")[0]
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_tag_job_completion
                    BEFORE UPDATE ON asset_tagging_jobs
                    WHEN NEW.status = 'succeeded'
                    BEGIN
                        SELECT RAISE(ABORT, 'reject completion');
                    END
                    """
                )

            with self.assertRaisesRegex(sqlite3.IntegrityError, "reject completion"):
                asset_tagging_jobs.complete(
                    db_path,
                    job_id=claimed["id"],
                    lease_token=claimed["lease_token"],
                    tags=["should rollback"],
                )

            with db.connect(db_path) as connection:
                metadata = json.loads(
                    connection.execute(
                        "SELECT metadata_json FROM assets WHERE id = ?", (asset_id,)
                    ).fetchone()["metadata_json"]
                )
                job = connection.execute(
                    "SELECT status, lease_token FROM asset_tagging_jobs WHERE id = ?",
                    (claimed["id"],),
                ).fetchone()
            self.assertEqual(metadata["tags"], ["manual"])
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["lease_token"], claimed["lease_token"])

    def test_failure_backoff_attempt_limit_and_explicit_retry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path)
            self._enqueue(db_path, asset_id=asset_id, max_attempts=2)
            start = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
            first = asset_tagging_jobs.claim(db_path, owner="worker", now=start)[0]
            failed = asset_tagging_jobs.fail(
                db_path,
                job_id=first["id"],
                lease_token=first["lease_token"],
                error_code="llm_timeout",
                error="Timed out",
                now=start,
            )
            self.assertTrue(failed["retryable"])
            self.assertEqual(
                failed["next_attempt_at"], (start + timedelta(seconds=60)).isoformat()
            )
            self.assertEqual(asset_tagging_jobs.claim(db_path, owner="worker", now=start), [])

            second = asset_tagging_jobs.claim(
                db_path, owner="worker", now=start + timedelta(seconds=60)
            )[0]
            exhausted = asset_tagging_jobs.fail(
                db_path,
                job_id=second["id"],
                lease_token=second["lease_token"],
                error_code="llm_timeout",
                error="Timed out again",
                now=start + timedelta(seconds=60),
            )
            self.assertFalse(exhausted["retryable"])
            self.assertIsNone(exhausted["next_attempt_at"])
            self.assertEqual(
                asset_tagging_jobs.claim(
                    db_path, owner="worker", now=start + timedelta(days=1)
                ),
                [],
            )

            retried = asset_tagging_jobs.retry(db_path, asset_id=asset_id)
            self.assertEqual(retried["status"], "pending")
            self.assertEqual(retried["attempt_count"], 0)
            self.assertTrue(retried["retryable"])

    def test_failure_does_not_persist_secret_bearing_exception_text(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path)
            self._enqueue(db_path, asset_id=asset_id)
            claimed = asset_tagging_jobs.claim(db_path, owner="worker")[0]

            failed = asset_tagging_jobs.fail(
                db_path,
                job_id=claimed["id"],
                lease_token=claimed["lease_token"],
                error_code="llm_api_error",
                error=(
                    "Authorization: Bearer secret-token "
                    "https://user:pass@example.invalid/v1 /data/private/session.txt"
                ),
            )

            self.assertEqual(failed["last_error"], "The language model request failed.")
            serialized = json.dumps(failed)
            self.assertNotIn("secret-token", serialized)
            self.assertNotIn("example.invalid", serialized)
            self.assertNotIn("/data/private", serialized)

    def test_asset_merge_collapses_jobs_and_invalidates_leases(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            canonical_id = self._asset(db_path, suffix="canonical", tags=["one"])
            merged_id = self._asset(db_path, suffix="merged", tags=["two"])
            canonical_job = self._enqueue(db_path, asset_id=canonical_id)
            self._enqueue(db_path, asset_id=merged_id)
            claimed = asset_tagging_jobs.claim(db_path, owner="worker", limit=2)
            merged_claim = next(job for job in claimed if job["asset_id"] == merged_id)
            now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC).isoformat()

            with db.connect(db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                assets._merge_assets(
                    connection,
                    canonical_id=canonical_id,
                    merged_id=merged_id,
                    now=now,
                )

            with db.connect(db_path) as connection:
                jobs = connection.execute("SELECT * FROM asset_tagging_jobs").fetchall()
                canonical = assets.get_asset(connection, canonical_id)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["id"], canonical_job["id"])
            self.assertEqual(jobs[0]["asset_id"], canonical_id)
            self.assertEqual(jobs[0]["status"], "pending")
            self.assertEqual(jobs[0]["attempt_count"], 0)
            self.assertIsNone(jobs[0]["lease_token"])
            self.assertEqual(canonical["metadata"]["tags"], ["one", "two"])
            with self.assertRaisesRegex(ValueError, "Unknown tagging job"):
                asset_tagging_jobs.complete(
                    db_path,
                    job_id=merged_claim["id"],
                    lease_token=merged_claim["lease_token"],
                    tags=["stale"],
                )

    def test_reserved_tags_and_stale_or_inactive_completion_are_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            asset_id = self._asset(db_path)
            self._enqueue(db_path, asset_id=asset_id)
            claimed = asset_tagging_jobs.claim(db_path, owner="worker")[0]
            with self.assertRaisesRegex(ValueError, "maintained automatically"):
                asset_tagging_jobs.complete(
                    db_path,
                    job_id=claimed["id"],
                    lease_token=claimed["lease_token"],
                    tags=["source:fake"],
                )
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE assets SET state = 'removed' WHERE id = ?", (asset_id,)
                )
            with self.assertRaisesRegex(ValueError, "active Asset"):
                asset_tagging_jobs.complete(
                    db_path,
                    job_id=claimed["id"],
                    lease_token=claimed["lease_token"],
                    tags=["safe"],
                )

    def _asset(
        self,
        db_path: Path,
        *,
        suffix: str = "one",
        state: str = "active",
        tags: list[str] | None = None,
    ) -> str:
        db.initialize_database(db_path)
        asset_id = f"asset_{suffix}"
        timestamp = datetime(2026, 9, 26, tzinfo=UTC).isoformat()
        with db.connect(db_path) as connection:
            connection.execute(
                """
                INSERT INTO assets (
                    id, media_type, state, metadata_json, created_at, updated_at
                ) VALUES (?, 'image', ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    state,
                    json.dumps({"tags": tags or []}),
                    timestamp,
                    timestamp,
                ),
            )
        return asset_id

    def _enqueue(
        self,
        db_path: Path,
        *,
        asset_id: str,
        max_attempts: int = 5,
    ) -> dict:
        return asset_tagging_jobs.enqueue(
            db_path,
            asset_id=asset_id,
            input_fingerprint="sha256:input",
            provider="openai_compatible",
            model="qwen3-8b",
            prompt_version="asset-tags-v1",
            max_attempts=max_attempts,
        )


if __name__ == "__main__":
    unittest.main()
